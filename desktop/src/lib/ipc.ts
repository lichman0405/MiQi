/**
 * IPC client for MiQi desktop backend.
 *
 * Provides a typed JSON-RPC 2.0 interface over the sidecar stdio transport.
 * The SidecarTransport launches the Python sidecar via Tauri's shell plugin
 * and communicates over newline-delimited JSON-RPC over stdio.
 * MockTransport is retained as a fallback for non-Tauri environments.
 */

// ── JSON-RPC protocol types ──────────────────────────────────────────────

export interface JsonRpcRequest {
  jsonrpc: "2.0";
  id: number;
  method: string;
  params?: Record<string, unknown>;
}

export interface JsonRpcResponse {
  jsonrpc: "2.0";
  id: number | null;
  result?: unknown;
  error?: {
    code: number;
    message: string;
    data?: unknown;
  };
}

export interface JsonRpcEvent {
  jsonrpc: "2.0";
  method: string;
  params?: Record<string, unknown>;
}

// ── Transport interface ──────────────────────────────────────────────────

export type TransportStatus = "connected" | "disconnected" | "connecting" | "restarting" | "mock";

export interface Transport {
  send(request: JsonRpcRequest): void;
  onResponse(handler: (response: JsonRpcResponse) => void): () => void;
  onEvent(handler: (event: JsonRpcEvent) => void): () => void;
  onStatusChange(handler: (status: TransportStatus) => void): () => void;
  close(): void;
  readonly status: TransportStatus;
}

// ── Sidecar transport (Tauri shell) ─────────────────────────────────────

/**
 * Internal handle for a running sidecar process.
 * Stores both the Command (for I/O events) and the Child (for stdin/kill).
 */
interface SidecarHandle {
  /** Write a line to the child's stdin. */
  write(data: string): Promise<void>;
  /** Kill the child process. */
  kill(): Promise<void>;
}

/**
 * Real IPC transport that communicates with the Python sidecar
 * via Tauri's shell plugin. The sidecar runs as:
 *   miqi desktop-backend --stdio
 *
 * Messages are newline-delimited JSON-RPC 2.0 over the sidecar's
 * stdin/stdout. stderr is reserved for the sidecar's own logging.
 */
export class SidecarTransport implements Transport {
  private _sidecarCommand: string;
  private _sidecarArgs: string[];
  private _responseHandlers = new Set<(response: JsonRpcResponse) => void>();
  private _eventHandlers = new Set<(event: JsonRpcEvent) => void>();
  private _statusHandlers = new Set<(status: TransportStatus) => void>();
  private _status: TransportStatus = "disconnected";
  private _handle: SidecarHandle | null = null;
  private _stdoutBuffer = "";
  private _closed = false;
  private _reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  /** Requests queued while the sidecar is connecting/reconnecting. */
  private _pendingQueue: JsonRpcRequest[] = [];

  constructor(sidecarCommand = "binaries/miqi-desktop-backend", sidecarArgs = ["--stdio"]) {
    this._sidecarCommand = sidecarCommand;
    this._sidecarArgs = sidecarArgs;
  }

  get status(): TransportStatus {
    return this._status;
  }

  private _setStatus(status: TransportStatus): void {
    if (this._status === status) return;
    this._status = status;
    for (const h of this._statusHandlers) h(status);
  }

  async connect(): Promise<void> {
    if (this._status === "connected" || this._status === "connecting") return;
    this._setStatus("connecting");

    try {
      const shell = await import("@tauri-apps/plugin-shell");
      const cmd = shell.Command.sidecar(this._sidecarCommand, this._sidecarArgs);

      // ── Register ALL event handlers BEFORE spawn() ──────────────────
      // Tauri fires events as soon as the process produces output.
      // Registering handlers after spawn() creates a race window where
      // early close/error events can be missed, causing silent failures.

      cmd.stdout.on("data", (chunk: string) => {
        this._stdoutBuffer += typeof chunk === "string" ? chunk : new TextDecoder().decode(chunk as unknown as ArrayBuffer);
        this._drainBuffer();
      });

      // Log stderr for diagnostics; never expose to UI (may contain config values)
      cmd.stderr.on("data", (line: string) => {
        if (line.trim()) console.debug("[MiQi sidecar stderr]", line.trimEnd());
      });

      cmd.on("close", (payload: { code: number | null; signal: number | null }) => {
        const prevHandle = this._handle;
        this._handle = null;
        if (this._closed) return;
        console.warn("[MiQi] Sidecar closed", payload);
        if (prevHandle !== null) {
          // Only reject/reconnect if we had previously connected successfully
          _rejectPendingRequests("Sidecar closed");
          this._setStatus("disconnected");
          this._scheduleReconnect();
        } else {
          // Closed before connect() finished — connect() will handle it
          this._setStatus("disconnected");
        }
      });

      cmd.on("error", (err: string) => {
        this._handle = null;
        if (this._closed) return;
        console.error("[MiQi] Sidecar error:", err);
        _rejectPendingRequests("Sidecar error");
        this._setStatus("disconnected");
        this._scheduleReconnect();
      });

      // ── Now spawn ───────────────────────────────────────────────────
      const child = await cmd.spawn();
      this._handle = {
        write: (data: string) => child.write(data),
        kill: () => child.kill(),
      };

      this._setStatus("connected");
      // Flush any requests that arrived while we were connecting
      const queued = this._pendingQueue.splice(0);
      for (const req of queued) this._doSend(req);
    } catch (e) {
      console.error("[MiQi] SidecarTransport.connect failed:", e);
      this._setStatus("disconnected");
    }
  }

  private _doSend(request: JsonRpcRequest): void {
    if (!this._handle) return;
    const line = JSON.stringify(request) + "\n";
    this._handle.write(line).catch(() => {
      for (const h of this._responseHandlers) {
        h({
          jsonrpc: "2.0",
          id: request.id,
          error: { code: -32000, message: "Failed to write to sidecar stdin" },
        });
      }
    });
  }

  send(request: JsonRpcRequest): void {
    if (this._status === "connecting" || this._status === "restarting") {
      // Queue the request; will be flushed once connected
      this._pendingQueue.push(request);
      return;
    }
    if (this._status !== "connected" || !this._handle) {
      for (const h of this._responseHandlers) {
        h({
          jsonrpc: "2.0",
          id: request.id,
          error: { code: -32000, message: "Sidecar not connected" },
        });
      }
      return;
    }
    this._doSend(request);
  }

  onResponse(handler: (response: JsonRpcResponse) => void): () => void {
    this._responseHandlers.add(handler);
    return () => { this._responseHandlers.delete(handler); };
  }

  onEvent(handler: (event: JsonRpcEvent) => void): () => void {
    this._eventHandlers.add(handler);
    return () => { this._eventHandlers.delete(handler); };
  }

  onStatusChange(handler: (status: TransportStatus) => void): () => void {
    this._statusHandlers.add(handler);
    return () => { this._statusHandlers.delete(handler); };
  }

  close(): void {
    this._closed = true;
    if (this._reconnectTimer !== null) {
      clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
    }
    if (this._handle) {
      this._handle.kill().catch(() => {});
      this._handle = null;
    }
    this._pendingQueue = [];
    _rejectPendingRequests("Sidecar transport closed");
    this._setStatus("disconnected");
    this._responseHandlers.clear();
    this._eventHandlers.clear();
    this._statusHandlers.clear();
  }

  async restart(): Promise<void> {
    this.close();
    this._closed = false;
    this._setStatus("restarting");
    await this.connect();
  }

  private _drainBuffer(): void {
    let newlineIdx: number;
    while ((newlineIdx = this._stdoutBuffer.indexOf("\n")) !== -1) {
      const line = this._stdoutBuffer.substring(0, newlineIdx).trim();
      this._stdoutBuffer = this._stdoutBuffer.substring(newlineIdx + 1);
      if (line) this._processLine(line);
    }
  }

  private _processLine(line: string): void {
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(line) as Record<string, unknown>;
    } catch {
      return;
    }

    if (parsed.jsonrpc !== "2.0") return;

    if ("id" in parsed && ("result" in parsed || "error" in parsed)) {
      const response: JsonRpcResponse = {
        jsonrpc: "2.0",
        id: parsed.id as number | null,
        ...(parsed.result !== undefined ? { result: parsed.result } : {}),
        ...(parsed.error !== undefined ? { error: parsed.error as JsonRpcResponse["error"] } : {}),
      };
      for (const h of this._responseHandlers) h(response);
    } else if ("method" in parsed && typeof parsed.method === "string") {
      const event: JsonRpcEvent = {
        jsonrpc: "2.0",
        method: parsed.method,
        ...(parsed.params !== undefined ? { params: parsed.params as Record<string, unknown> } : {}),
      };
      for (const h of this._eventHandlers) h(event);
    }
  }

  private _scheduleReconnect(): void {
    if (this._closed) return;
    if (this._reconnectTimer !== null) return;
    this._reconnectTimer = setTimeout(() => {
      this._reconnectTimer = null;
      if (!this._closed) this.connect();
    }, 3000);
  }
}

// ── Mock transport ───────────────────────────────────────────────────────

const MOCK_DELAY_MS = 80;

interface MockSessionInfo {
  key: string;
  title: string;
  updated_at: string;
  message_count: number;
  source: string;
  archived?: boolean;
  messages: Array<{ role: string; content: string; timestamp?: string }>;
}

let MOCK_SESSIONS: MockSessionInfo[] = [
  {
    key: "desktop:s1",
    title: "API design discussion",
    updated_at: "2025-05-08T14:00:00",
    message_count: 2,
    source: "desktop",
    messages: [
      { role: "user", content: "Sketch the API design." },
      { role: "assistant", content: "Here is a compact API shape." },
    ],
  },
  {
    key: "desktop:s2",
    title: "Bug fix: shell timeout",
    updated_at: "2025-05-08T10:30:00",
    message_count: 2,
    source: "desktop",
    messages: [
      { role: "user", content: "Fix the shell timeout." },
      { role: "assistant", content: "The timeout path is patched." },
    ],
  },
  {
    key: "desktop:s3",
    title: "Refactor config loader",
    updated_at: "2025-05-07T16:00:00",
    message_count: 1,
    source: "desktop",
    archived: true,
    messages: [
      { role: "user", content: "Refactor config loader." },
    ],
  },
];

function mockSessionSummary(session: MockSessionInfo) {
  return {
    key: session.key,
    title: session.title,
    updated_at: session.updated_at,
    message_count: session.message_count,
    source: session.source,
    ...(session.archived ? { archived: true } : {}),
  };
}

function mockSessionList(includeArchived: boolean) {
  const sessions = MOCK_SESSIONS
    .filter((s) => includeArchived || !s.archived)
    .map(mockSessionSummary)
    .sort((a, b) => b.updated_at.localeCompare(a.updated_at));
  return { sessions, count: sessions.length };
}

function mockSessionSearch(params?: Record<string, unknown>) {
  const query = String(params?.query ?? "").trim();
  if (!query) return { sessions: [], count: 0, query };
  const includeArchived = Boolean(params?.include_archived);
  const lower = query.toLowerCase();
  const sessions = MOCK_SESSIONS
    .filter((s) => includeArchived || !s.archived)
    .filter((s) =>
      s.title.toLowerCase().includes(lower)
      || s.messages.some((m) => m.content.toLowerCase().includes(lower))
    )
    .map(mockSessionSummary)
    .sort((a, b) => b.updated_at.localeCompare(a.updated_at));
  return { sessions, count: sessions.length, query };
}

function mockSessionByKey(key: string): MockSessionInfo {
  const session = MOCK_SESSIONS.find((s) => s.key === key);
  if (!session) throw new Error(`Session not found: ${key}`);
  return session;
}

interface MockWorkspaceEntry {
  name: string;
  path: string;
  is_dir: boolean;
  is_symlink: boolean;
  size: number;
  modified: string;
}

let MOCK_WORKSPACE_ROOT = "~/projects/miqi-demo";

const MOCK_WORKSPACE_ENTRIES: MockWorkspaceEntry[] = [
  { name: "desktop", path: "desktop", is_dir: true, is_symlink: false, size: 0, modified: "2025-05-08T10:00:00" },
  { name: "src", path: "desktop/src", is_dir: true, is_symlink: false, size: 0, modified: "2025-05-08T10:00:00" },
  { name: "App.tsx", path: "desktop/src/App.tsx", is_dir: false, is_symlink: false, size: 8420, modified: "2025-05-08T10:12:00" },
  { name: "components", path: "desktop/src/components", is_dir: true, is_symlink: false, size: 0, modified: "2025-05-08T10:11:00" },
  { name: "ListPanel.tsx", path: "desktop/src/components/ListPanel.tsx", is_dir: false, is_symlink: false, size: 12600, modified: "2025-05-08T10:15:00" },
  { name: "miqi", path: "miqi", is_dir: true, is_symlink: false, size: 0, modified: "2025-05-08T09:00:00" },
  { name: "ipc", path: "miqi/ipc", is_dir: true, is_symlink: false, size: 0, modified: "2025-05-08T09:20:00" },
  { name: "handlers.py", path: "miqi/ipc/handlers.py", is_dir: false, is_symlink: false, size: 18100, modified: "2025-05-08T09:25:00" },
  { name: "README.md", path: "README.md", is_dir: false, is_symlink: false, size: 2800, modified: "2025-05-07T18:00:00" },
];

const MOCK_FILE_CONTENT: Record<string, string> = {
  "README.md": "# MiQi\n\nMock workspace preview rendered through workspace.preview.",
  "desktop/src/App.tsx": "export function App() {\n  return <DesktopShell />;\n}\n",
  "desktop/src/components/ListPanel.tsx": "export function ListPanel() {\n  return <aside />;\n}\n",
  "miqi/ipc/handlers.py": "class RpcDispatcher:\n    async def _workspace_index(self, params):\n        return self._runtime.workspace_service.index()\n",
};

let MOCK_PINNED_FILES = ["README.md", "desktop/src/App.tsx"];
let MOCK_RECENT_FILES = ["miqi/ipc/handlers.py", "desktop/src/components/ListPanel.tsx", "README.md"];
let MOCK_MEMORY_SNAPSHOT = [
  {
    id: "mem-1",
    text: "Desktop UI uses stdio JSON-RPC through the Python sidecar.",
    session_key: "desktop:s1",
    source: "desktop",
    hits: 3,
    updated_at: "2025-05-08T10:00:00",
  },
];
let MOCK_MEMORY_LESSONS = [
  {
    id: "lesson-1",
    trigger: "When wiring desktop UI",
    better_action: "Use typed JSON-RPC helpers and contract probes.",
    bad_action: "",
    confidence: 2,
    enabled: true,
    source: "desktop",
    updated_at: "2025-05-08T11:00:00",
  },
];
let MOCK_DAILY_NOTES = [
  { date: "2025-05-08", excerpt: "Shipped session and workspace desktop foundations." },
];
let MOCK_CRON_JOBS: Array<{
  id: string;
  name: string;
  enabled: boolean;
  schedule: {
    kind: string;
    at_ms?: number | null;
    every_ms?: number | null;
    expr?: string | null;
    tz?: string | null;
  };
  payload: {
    kind: string;
    message: string;
    deliver: boolean;
    channel?: string | null;
    to?: string | null;
  };
  state: {
    next_run_at_ms?: number | null;
    last_run_at_ms?: number | null;
    last_status?: string | null;
    last_error?: string | null;
  };
  created_at_ms: number;
}> = [];
let MOCK_HEARTBEAT = { enabled: true, interval_seconds: 1800, running: true };

function mockWorkspaceEntry(path: string): MockWorkspaceEntry | undefined {
  return MOCK_WORKSPACE_ENTRIES.find((entry) => entry.path === path);
}

function mockWorkspaceFileInfo(path: string) {
  const entry = mockWorkspaceEntry(path);
  return {
    path,
    exists: Boolean(entry),
    is_dir: entry?.is_dir ?? false,
    is_symlink: entry?.is_symlink ?? false,
    is_binary: false,
    size: entry?.size ?? 0,
    modified: entry?.modified ?? "",
  };
}

function mockWorkspaceIndex(params?: Record<string, unknown>) {
  const subdir = typeof params?.subdir === "string" ? params.subdir : "";
  const depth = Number(params?.depth ?? 6);
  const prefix = subdir ? `${subdir}/` : "";
  const entries = MOCK_WORKSPACE_ENTRIES.filter((entry) => {
    if (!subdir) return true;
    return entry.path.startsWith(prefix);
  }).filter((entry) => entry.path.split("/").length <= Math.max(1, depth));
  return {
    root: MOCK_WORKSPACE_ROOT,
    subdir,
    entries,
    count: entries.length,
    truncated: false,
  };
}

function mockWorkspaceOpen(params?: Record<string, unknown>) {
  const path = String(params?.path ?? "").trim();
  if (!path) throw new Error("params.path is required");
  MOCK_WORKSPACE_ROOT = path;
  MOCK_PINNED_FILES = [];
  MOCK_RECENT_FILES = [];
  return {
    project_root: MOCK_WORKSPACE_ROOT,
    exists: true,
    restrict_to_workspace: true,
    pinned_count: 0,
    recent_count: 0,
  };
}

function mockWorkspacePreview(params?: Record<string, unknown>) {
  const path = String(params?.path ?? "");
  if (!path) throw new Error("params.path is required");
  const entry = mockWorkspaceEntry(path);
  if (!entry) return { path, exists: false };
  if (entry.is_dir) return { path, exists: true, is_dir: true };
  const content = MOCK_FILE_CONTENT[path] ?? `Preview for ${path}`;
  return {
    path,
    exists: true,
    is_dir: false,
    is_binary: false,
    size: entry.size,
    truncated: false,
    content,
  };
}

function mockWorkspacePin(params?: Record<string, unknown>) {
  const path = String(params?.path ?? "");
  if (!path) throw new Error("params.path is required");
  const entry = mockWorkspaceEntry(path);
  if (!entry || entry.is_dir) throw new Error(`file does not exist: ${path}`);
  if (!MOCK_PINNED_FILES.includes(path)) MOCK_PINNED_FILES = [...MOCK_PINNED_FILES, path];
  return { path, pinned: true };
}

function mockWorkspaceUnpin(params?: Record<string, unknown>) {
  const path = String(params?.path ?? "");
  if (!path) throw new Error("params.path is required");
  MOCK_PINNED_FILES = MOCK_PINNED_FILES.filter((file) => file !== path);
  return { path, pinned: false };
}

function mockWorkspacePinned() {
  const files = MOCK_PINNED_FILES.map(mockWorkspaceFileInfo);
  return { files, count: files.length };
}

function mockWorkspaceRecent(params?: Record<string, unknown>) {
  const limit = Number(params?.limit ?? 20);
  const files = MOCK_RECENT_FILES.slice(0, limit).map(mockWorkspaceFileInfo);
  return { files, count: files.length };
}

function mockMemoryStatus() {
  return {
    ltm_items: MOCK_MEMORY_SNAPSHOT.length,
    snapshot_exists: MOCK_MEMORY_SNAPSHOT.length > 0,
    lessons_count: MOCK_MEMORY_LESSONS.length,
    self_improvement_enabled: true,
    short_term_sessions: MOCK_SESSIONS.length,
    pending_sessions: 0,
    dirty_updates: 0,
  };
}

function mockMemorySearch(params?: Record<string, unknown>) {
  const query = String(params?.query ?? "").trim();
  const limit = Number(params?.limit ?? 20);
  if (!query) return { results: [], count: 0, query };
  const lower = query.toLowerCase();
  const results = [
    ...MOCK_MEMORY_SNAPSHOT
      .filter((item) => item.text.toLowerCase().includes(lower))
      .map((item) => ({
        source: "snapshot",
        id: item.id,
        text: item.text,
        hits: item.hits,
        updated_at: item.updated_at,
      })),
    ...MOCK_MEMORY_LESSONS
      .filter((lesson) =>
        lesson.trigger.toLowerCase().includes(lower)
        || lesson.better_action.toLowerCase().includes(lower)
      )
      .map((lesson) => ({
        source: "lesson",
        id: lesson.id,
        trigger: lesson.trigger,
        better_action: lesson.better_action,
        confidence: lesson.confidence,
        enabled: lesson.enabled,
      })),
    ...MOCK_DAILY_NOTES
      .filter((note) => note.excerpt.toLowerCase().includes(lower))
      .map((note) => ({ source: "daily_note", date: note.date, excerpt: note.excerpt })),
  ].slice(0, limit);
  return { results, count: results.length, query };
}

function mockMemoryRemember(text: string) {
  if (!text.trim()) throw new Error("params.text is required");
  MOCK_MEMORY_SNAPSHOT = [
    {
      id: `mem-${Date.now()}`,
      text: text.trim(),
      session_key: "desktop:default",
      source: "desktop",
      hits: 0,
      updated_at: new Date().toISOString(),
    },
    ...MOCK_MEMORY_SNAPSHOT,
  ];
  return { action: "remember", text_length: text.trim().length };
}

function mockMemoryUpdate(params?: Record<string, unknown>) {
  const text = String(params?.text ?? "");
  const action = String(params?.action ?? "remember");
  if (!text.trim()) throw new Error("params.text is required");
  if (action === "remember") return mockMemoryRemember(text);
  if (action === "append_today") return mockMemoryAppendToday(text);
  if (action === "learn_lesson") {
    return mockMemoryLearnLesson({
      trigger: text,
      better_action: String(params?.better_action ?? text),
      bad_action: String(params?.bad_action ?? ""),
    });
  }
  throw new Error(`params.action must be 'remember', 'append_today', or 'learn_lesson', got '${action}'`);
}

function mockMemoryAppendToday(content: string) {
  if (!content.trim()) throw new Error("params.content is required");
  const date = new Date().toISOString().slice(0, 10);
  MOCK_DAILY_NOTES = [{ date, excerpt: content.trim() }, ...MOCK_DAILY_NOTES];
  return { action: "append_today", date };
}

function mockMemoryLearnLesson(params?: Record<string, unknown>) {
  const trigger = String(params?.trigger ?? "");
  const betterAction = String(params?.better_action ?? "");
  if (!trigger.trim()) throw new Error("params.trigger is required");
  if (!betterAction.trim()) throw new Error("params.better_action is required");
  MOCK_MEMORY_LESSONS = [
    {
      id: `lesson-${Date.now()}`,
      trigger: trigger.trim(),
      better_action: betterAction.trim(),
      bad_action: String(params?.bad_action ?? ""),
      confidence: 1,
      enabled: true,
      source: "desktop",
      updated_at: new Date().toISOString(),
    },
    ...MOCK_MEMORY_LESSONS,
  ];
  return { action: "learn_lesson", trigger_length: trigger.trim().length };
}

function mockCronList(params?: Record<string, unknown>) {
  const includeDisabled = Boolean(params?.include_disabled);
  const jobs = MOCK_CRON_JOBS.filter((job) => includeDisabled || job.enabled);
  return { jobs, count: jobs.length };
}

function mockCronAdd(params?: Record<string, unknown>) {
  const name = String(params?.name ?? "").trim();
  const message = String(params?.message ?? "").trim();
  const schedule = params?.schedule as Record<string, unknown> | undefined;
  if (!name) throw new Error("params.name is required");
  if (!message) throw new Error("params.message is required");
  const now = Date.now();
  const kind = String(schedule?.kind ?? "every");
  const everyMs = typeof schedule?.every_ms === "number" ? schedule.every_ms : null;
  const atMs = typeof schedule?.at_ms === "number" ? schedule.at_ms : null;
  const job = {
    id: `cron-${now}`,
    name,
    enabled: true,
    schedule: { kind, every_ms: everyMs, at_ms: atMs, expr: null, tz: null },
    payload: { kind: "agent_turn", message, deliver: false, channel: null, to: null },
    state: {
      next_run_at_ms: kind === "every" && everyMs ? now + everyMs : atMs,
      last_run_at_ms: null,
      last_status: null,
      last_error: null,
    },
    created_at_ms: now,
  };
  MOCK_CRON_JOBS = [...MOCK_CRON_JOBS, job];
  return { success: true, job_id: job.id };
}

function mockCronUpdate(params?: Record<string, unknown>) {
  const jobId = String(params?.job_id ?? "");
  if (!jobId) throw new Error("params.job_id is required");
  const job = MOCK_CRON_JOBS.find((item) => item.id === jobId);
  if (!job) throw new Error(`Job '${jobId}' not found`);
  if ("enabled" in (params ?? {})) {
    job.enabled = Boolean(params?.enabled);
    job.state.next_run_at_ms = job.enabled
      ? Date.now() + (job.schedule.every_ms ?? 0)
      : null;
  }
  return { success: true, job_id: jobId };
}

function mockCronDelete(params?: Record<string, unknown>) {
  const jobId = String(params?.job_id ?? "");
  if (!jobId) throw new Error("params.job_id is required");
  const before = MOCK_CRON_JOBS.length;
  MOCK_CRON_JOBS = MOCK_CRON_JOBS.filter((job) => job.id !== jobId);
  return { success: MOCK_CRON_JOBS.length !== before, job_id: jobId };
}

function mockConfigWrite(params?: Record<string, unknown>): { success: boolean } {
  const updates = params?.updates;
  if (!updates || typeof updates !== "object" || Array.isArray(updates)) {
    throw new Error("params.updates is required and must be a dict");
  }

  for (const key of Object.keys(updates as Record<string, unknown>)) {
    if (key.includes(".")) {
      throw new Error(`Unknown config key: ${key}`);
    }
  }

  return { success: true };
}

const MOCK_HANDLERS: Record<string, (params?: Record<string, unknown>) => unknown> = {
  "app.status": () => ({
    status: "running",
    model: "anthropic/claude-sonnet-4-5",
    workspace: "~/projects/miqi-demo",
    agent_name: "miqi",
  }),
  "config.read": () => ({
    agents: {
      defaults: { name: "miqi", model: "anthropic/claude-sonnet-4-5", workspace: "~/.miqi/workspace", maxTokens: 8192, temperature: 0.1 },
    },
    providers: {
      anthropic: { apiKey: "********", apiBase: "" },
      openai: { apiKey: "", apiBase: "" },
    },
    tools: { restrictToWorkspace: true },
    heartbeat: { enabled: true, intervalSeconds: 1800 },
  }),
  "config.write": mockConfigWrite,
  "config.testProvider": (params) => {
    const provider = (params?.provider as string) ?? "unknown";
    return { success: true, model: `${provider}/test-model`, preview: "Mock test successful." };
  },
  "workspace.status": () => ({
    project_root: MOCK_WORKSPACE_ROOT,
    exists: true,
    restrict_to_workspace: true,
    pinned_count: MOCK_PINNED_FILES.length,
    recent_count: MOCK_RECENT_FILES.length,
  }),
  "workspace.open": mockWorkspaceOpen,
  "workspace.index": mockWorkspaceIndex,
  "workspace.preview": mockWorkspacePreview,
  "workspace.pinFile": mockWorkspacePin,
  "workspace.unpinFile": mockWorkspaceUnpin,
  "workspace.listPinned": mockWorkspacePinned,
  "workspace.listRecent": mockWorkspaceRecent,
  "context.status": () => ({
    workspace: MOCK_WORKSPACE_ROOT,
    bootstrap_files: [
      { name: "TOOLS.md", exists: true, source: "system", has_workspace_override: false, size: 4800 },
      { name: "AGENTS.md", exists: true, source: "workspace", has_workspace_override: false, size: 1200 },
      { name: "CLAUDE.md", exists: false, source: "none", has_workspace_override: false, size: 0 },
    ],
    skills: [
      { name: "memory", description: "Memory context helpers", available: true },
      { name: "workspace", description: "Workspace file inspection", available: true },
    ],
    memory: {
      ltm_items: 5,
      lessons_count: 3,
      self_improvement_enabled: true,
      snapshot_exists: true,
    },
    pinned_files: {
      count: MOCK_PINNED_FILES.length,
      files: [...MOCK_PINNED_FILES],
    },
    budget: {
      context_limit_chars: 600000,
      estimated_usage: 73000,
    },
  }),
  "context.listBootstrap": () => ({
    files: [
      { name: "TOOLS.md", exists: true, source: "system", has_workspace_override: false, size: 4800 },
      { name: "AGENTS.md", exists: true, source: "workspace", has_workspace_override: false, size: 1200 },
      { name: "CLAUDE.md", exists: false, source: "none", has_workspace_override: false, size: 0 },
    ],
    count: 3,
  }),
  "context.readBootstrap": (params) => {
    const name = String(params?.name ?? "");
    if (!name) throw new Error("params.name is required");
    const files: Record<string, { exists: boolean; source: string; size: number; content: string | null }> = {
      "TOOLS.md": {
        exists: true,
        source: "system",
        size: 4800,
        content: "Mock TOOLS.md content from the system bootstrap template.",
      },
      "AGENTS.md": {
        exists: true,
        source: "workspace",
        size: 1200,
        content: "Mock AGENTS.md workspace bootstrap content.",
      },
      "CLAUDE.md": {
        exists: false,
        source: "none",
        size: 0,
        content: null,
      },
    };
    const file = files[name];
    if (!file) throw new Error(`unknown bootstrap file: ${name}`);
    return {
      name,
      exists: file.exists,
      source: file.source,
      has_workspace_override: false,
      size: file.size,
      content: file.content,
      truncated: false,
    };
  },
  "context.listSkills": () => ({
    skills: [
      { name: "memory", description: "Memory context helpers", available: true },
      { name: "workspace", description: "Workspace file inspection", available: true },
    ],
    count: 2,
  }),
  "session.list": (params) => mockSessionList(Boolean(params?.include_archived)),
  "session.search": mockSessionSearch,
  "session.create": (params) => {
    const key = String(params?.key ?? "desktop:new");
    const title = String(params?.title ?? "New chat");
    const existing = MOCK_SESSIONS.find((s) => s.key === key);
    if (existing) return mockSessionSummary(existing);
    const session: MockSessionInfo = {
      key,
      title,
      source: key.includes(":") ? key.split(":", 1)[0] ?? "desktop" : "unknown",
      updated_at: new Date().toISOString(),
      message_count: 0,
      messages: [],
    };
    MOCK_SESSIONS = [session, ...MOCK_SESSIONS];
    return mockSessionSummary(session);
  },
  "session.rename": (params) => {
    const key = String(params?.key ?? "");
    const title = String(params?.title ?? "").trim();
    if (!key) throw new Error("params.key is required");
    if (!title) throw new Error("params.title is required");
    const session = mockSessionByKey(key);
    session.title = title;
    session.updated_at = new Date().toISOString();
    return { key, title };
  },
  "session.archive": (params) => {
    const key = String(params?.key ?? "");
    if (!key) throw new Error("params.key is required");
    mockSessionByKey(key).archived = true;
    return { key, archived: true };
  },
  "session.unarchive": (params) => {
    const key = String(params?.key ?? "");
    if (!key) throw new Error("params.key is required");
    delete mockSessionByKey(key).archived;
    return { key, archived: false };
  },
  "session.delete": (params) => {
    const key = String(params?.key ?? "");
    if (!key) throw new Error("params.key is required");
    const before = MOCK_SESSIONS.length;
    MOCK_SESSIONS = MOCK_SESSIONS.filter((s) => s.key !== key);
    return { key, deleted: MOCK_SESSIONS.length !== before };
  },
  "session.load": (params) => {
    const key = String(params?.key ?? "desktop:s1");
    const session = mockSessionByKey(key);
    return {
      key: session.key,
      title: session.title,
      source: session.source,
      updated_at: session.updated_at,
      message_count: session.message_count,
      messages: session.messages,
    };
  },
  "chat.send": () => ({ execution_id: "exec-mock-001" }),
  "chat.cancel": (params) => ({ success: true, execution_id: params?.execution_id ?? "" }),
  "chat.regenerate": () => ({ execution_id: "exec-mock-regen" }),
  "chat.approve": (params) => ({ success: true, approval_id: params?.approval_id ?? "", decision: params?.choice ?? "once" }),
  "chat.deny": (params) => ({ success: true, approval_id: params?.approval_id ?? "", decision: "deny" }),
  "tool.list": () => ({ tools: [{ name: "read_file" }, { name: "write_file" }, { name: "exec" }], count: 3 }),
  "mcp.status": () => ({
    connected: false,
    connecting: false,
    servers: {
      "mock-filesystem": { configured: true, connected: false },
    },
    retry_after: 0,
  }),
  "memory.status": mockMemoryStatus,
  "memory.search": mockMemorySearch,
  "memory.update": mockMemoryUpdate,
  "memory.remember": (params) => mockMemoryRemember(String(params?.text ?? "")),
  "memory.appendToday": (params) => mockMemoryAppendToday(String(params?.content ?? "")),
  "memory.learnLesson": mockMemoryLearnLesson,
  "memory.listSnapshot": (params) => {
    const limit = Number(params?.limit ?? 50);
    const items = MOCK_MEMORY_SNAPSHOT.slice(0, limit);
    return { items, count: items.length };
  },
  "memory.listLessons": (params) => {
    const includeDisabled = Boolean(params?.include_disabled);
    const limit = Number(params?.limit ?? 50);
    const lessons = MOCK_MEMORY_LESSONS
      .filter((lesson) => includeDisabled || lesson.enabled)
      .slice(0, limit);
    return { lessons, count: lessons.length };
  },
  "memory.deleteSnapshotItem": (params) => {
    const itemId = String(params?.item_id ?? "");
    if (!itemId) throw new Error("params.item_id is required");
    MOCK_MEMORY_SNAPSHOT = MOCK_MEMORY_SNAPSHOT.filter((item) => item.id !== itemId);
    return { action: "delete_snapshot_item", item_id: itemId };
  },
  "memory.deleteLesson": (params) => {
    const lessonId = String(params?.lesson_id ?? "");
    if (!lessonId) throw new Error("params.lesson_id is required");
    MOCK_MEMORY_LESSONS = MOCK_MEMORY_LESSONS.filter((lesson) => lesson.id !== lessonId);
    return { action: "delete_lesson", lesson_id: lessonId };
  },
  "memory.setLessonEnabled": (params) => {
    const lessonId = String(params?.lesson_id ?? "");
    if (!lessonId) throw new Error("params.lesson_id is required");
    const lesson = MOCK_MEMORY_LESSONS.find((item) => item.id === lessonId);
    if (lesson) lesson.enabled = Boolean(params?.enabled);
    return { action: "set_lesson_enabled", lesson_id: lessonId, enabled: Boolean(params?.enabled) };
  },
  "cron.list": mockCronList,
  "cron.add": mockCronAdd,
  "cron.update": mockCronUpdate,
  "cron.delete": mockCronDelete,
  "heartbeat.status": () => ({ ...MOCK_HEARTBEAT }),
  "heartbeat.update": (params) => {
    if ("enabled" in (params ?? {})) MOCK_HEARTBEAT.enabled = Boolean(params?.enabled);
    if ("interval_seconds" in (params ?? {})) {
      MOCK_HEARTBEAT.interval_seconds = Number(params?.interval_seconds ?? MOCK_HEARTBEAT.interval_seconds);
    }
    return { success: true };
  },
};

export class MockTransport implements Transport {
  private _responseHandlers = new Set<(response: JsonRpcResponse) => void>();
  private _eventHandlers = new Set<(event: JsonRpcEvent) => void>();
  private _statusHandlers = new Set<(status: TransportStatus) => void>();
  private _status: TransportStatus = "mock";

  get status(): TransportStatus {
    return this._status;
  }

  send(request: JsonRpcRequest): void {
    const handler = MOCK_HANDLERS[request.method];
    setTimeout(() => {
      if (handler) {
        let result: unknown;
        try {
          result = handler(request.params);
        } catch (err) {
          const message = err instanceof Error ? err.message : String(err);
          for (const h of this._responseHandlers) {
            h({
              jsonrpc: "2.0",
              id: request.id,
              error: { code: -32603, message: `Internal error: ${message}` },
            });
          }
          return;
        }
        for (const h of this._responseHandlers) h({ jsonrpc: "2.0", id: request.id, result });
        // Emit mock runtime events for chat.send to drive the UI
        if (request.method === "chat.send") {
          this._emitMockRunEvents(result as { execution_id: string });
        }
        this._emitMockStateChangeEvent(request.method, result);
      } else {
        for (const h of this._responseHandlers) {
          h({
            jsonrpc: "2.0",
            id: request.id,
            error: { code: -32601, message: `Method not found: ${request.method}` },
          });
        }
      }
    }, MOCK_DELAY_MS);
  }

  private _emitMockStateChangeEvent(method: string, result: unknown): void {
    const emit = (methodName: string, params: Record<string, unknown>) => {
      for (const h of this._eventHandlers) {
        h({ jsonrpc: "2.0", method: methodName, params });
      }
    };

    const memoryActions = new Set([
      "memory.update",
      "memory.remember",
      "memory.appendToday",
      "memory.learnLesson",
      "memory.deleteSnapshotItem",
      "memory.deleteLesson",
      "memory.setLessonEnabled",
    ]);
    if (memoryActions.has(method)) {
      const lessonActions = new Set([
        "memory.learnLesson",
        "memory.deleteLesson",
        "memory.setLessonEnabled",
      ]);
      emit("MemoryChanged", { action: lessonActions.has(method) ? "lesson" : "snapshot" });
      return;
    }

    if (method === "cron.add" || method === "cron.update" || method === "cron.delete") {
      const payload = result as { job_id?: string; success?: boolean };
      if (method === "cron.delete" && payload.success === false) return;
      emit("CronJobChanged", {
        job_id: payload.job_id ?? "",
        action: method === "cron.add" ? "added" : method === "cron.delete" ? "deleted" : "updated",
      });
    }
  }

  private _emitMockRunEvents(result: { execution_id: string }): void {
    const execId = result.execution_id;
    const emit = (type: string, params: Record<string, unknown>, delayMs: number) => {
      setTimeout(() => {
        for (const h of this._eventHandlers) {
          h({ jsonrpc: "2.0", method: type, params: { ...params, execution_id: execId } });
        }
      }, delayMs);
    };

    emit("RunStarted", { session_key: "desktop:default", preview: "mock message" }, 50);
    emit("ToolCallStarted", { tool_name: "read_file", tool_call_id: "tc-mock-1" }, 200);
    emit("ToolResult", { tool_name: "read_file", tool_call_id: "tc-mock-1", preview: "file contents...", is_error: false }, 500);
    emit("MessageDelta", { delta: "Here is " }, 600);
    emit("MessageDelta", { delta: "the mock response." }, 900);
    emit("MessageFinal", { content: "Here is the mock response." }, 1100);
    emit("RunCompleted", { session_key: "desktop:default", response_preview: "Here is the mock response." }, 1200);
  }

  onResponse(handler: (response: JsonRpcResponse) => void): () => void {
    this._responseHandlers.add(handler);
    return () => { this._responseHandlers.delete(handler); };
  }

  onEvent(handler: (event: JsonRpcEvent) => void): () => void {
    this._eventHandlers.add(handler);
    return () => { this._eventHandlers.delete(handler); };
  }

  onStatusChange(handler: (status: TransportStatus) => void): () => void {
    this._statusHandlers.add(handler);
    return () => { this._statusHandlers.delete(handler); };
  }

  close(): void {
    this._responseHandlers.clear();
    this._eventHandlers.clear();
    this._statusHandlers.clear();
  }
}

// ── IPC Client (singleton) ───────────────────────────────────────────────

let _nextId = 1;

const pendingRequests = new Map<number, {
  resolve: (value: unknown) => void;
  reject: (reason: unknown) => void;
}>();

let _transport: Transport = new MockTransport();
let _unsubscribe: (() => void) | null = null;

function _handleResponse(response: JsonRpcResponse): void {
  if (response.id === null) return;
  const pending = pendingRequests.get(response.id);
  if (!pending) return;
  pendingRequests.delete(response.id);
  if (response.error) {
    pending.reject(new IpcError(response.error.code, response.error.message, response.error.data));
  } else {
    pending.resolve(response.result);
  }
}

function _rejectPendingRequests(reason: string): void {
  for (const [id, pending] of pendingRequests) {
    pendingRequests.delete(id);
    pending.reject(new IpcError(-32000, reason));
  }
}

export class IpcError extends Error {
  code: number;
  data?: unknown;

  constructor(code: number, message: string, data?: unknown) {
    super(message);
    this.name = "IpcError";
    this.code = code;
    this.data = data;
  }
}

export function setTransport(transport: Transport): void {
  _unsubscribe?.();
  _rejectPendingRequests("Transport replaced");
  _transport = transport;
  _unsubscribe = transport.onResponse(_handleResponse);
}

export function getTransport(): Transport {
  return _transport;
}

export function getTransportStatus(): TransportStatus {
  return _transport.status;
}

export async function request<T = unknown>(
  method: string,
  params?: Record<string, unknown>,
): Promise<T> {
  const id = _nextId++;
  const rpcRequest: JsonRpcRequest = {
    jsonrpc: "2.0",
    id,
    method,
    ...(params ? { params } : {}),
  };

  return new Promise<T>((resolve, reject) => {
    pendingRequests.set(id, {
      resolve: resolve as (value: unknown) => void,
      reject,
    });
    _transport.send(rpcRequest);
  });
}

// ── Event subscriber ─────────────────────────────────────────────────────

type EventHandler = (event: JsonRpcEvent) => void;

const _eventSubscribers = new Set<EventHandler>();
let _eventUnsub: (() => void) | null = null;

function _forwardEvents(): void {
  _eventUnsub?.();
  _eventUnsub = _transport.onEvent((event) => {
    for (const h of _eventSubscribers) h(event);
  });
}

export function subscribe(handler: EventHandler): () => void {
  if (_eventSubscribers.size === 0) _forwardEvents();
  _eventSubscribers.add(handler);
  return () => {
    _eventSubscribers.delete(handler);
    if (_eventSubscribers.size === 0) {
      _eventUnsub?.();
      _eventUnsub = null;
    }
  };
}

// ── Transport status subscriber ──────────────────────────────────────────

type StatusHandler = (status: TransportStatus) => void;

const _statusSubscribers = new Set<StatusHandler>();
let _statusUnsub: (() => void) | null = null;

function _forwardStatus(): void {
  _statusUnsub?.();
  _statusUnsub = _transport.onStatusChange((status) => {
    for (const h of _statusSubscribers) h(status);
  });
}

export function onStatusChange(handler: StatusHandler): () => void {
  if (_statusSubscribers.size === 0) _forwardStatus();
  _statusSubscribers.add(handler);
  return () => {
    _statusSubscribers.delete(handler);
    if (_statusSubscribers.size === 0) {
      _statusUnsub?.();
      _statusUnsub = null;
    }
  };
}

// ── Auto-detect and initialize transport ─────────────────────────────────

/**
 * Detect whether we're running inside Tauri and initialize the
 * appropriate transport.  Falls back to MockTransport when Tauri
 * is unavailable (plain Vite dev server, test environment).
 *
 * This should be called once at app startup.
 */
export async function initTransport(): Promise<TransportStatus> {
  const sidecar = new SidecarTransport();

  // Replace transport immediately so all requests during startup are queued in the sidecar.
  // SidecarTransport queues requests while status is "connecting" and flushes on connect.
  setTransport(sidecar);
  _forwardEvents();
  _forwardStatus();

  await sidecar.connect();

  if (sidecar.status === "connected") {
    // Notify status subscribers that we're connected
    for (const h of _statusSubscribers) h("connected");
    return "connected";
  }

  // Sidecar not available — fall back to mock
  sidecar.close();
  const mock = new MockTransport();
  setTransport(mock);
  _forwardEvents();
  _forwardStatus();
  return "mock";
}

// Initialize with MockTransport as default (will be upgraded by initTransport)
setTransport(_transport);
