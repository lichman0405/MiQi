import { useEffect, useState } from "react";
import {
  useContextBootstrap,
  useContextBootstrapPreview,
  useContextSkills,
  useContextStatus,
  useToolList,
  useMemoryStatus,
  useMemorySnapshot,
  useMemoryLessons,
  useMcpStatus,
  useWorkspacePinned,
  useWorkspacePreview,
  useWorkspaceRecent,
} from "../lib/hooks";
import { subscribeRuntimeEvents, type RuntimeEvent } from "../lib/chat-state";
import type { ContextBootstrapFile, ContextSkill, WorkspaceFileInfo, WorkspacePreviewResult } from "../lib/workspace-state";
import "./Inspector.css";

type InspectorTab = "context" | "activity" | "files" | "memory" | "tools";

const TABS: { id: InspectorTab; label: string }[] = [
  { id: "context", label: "Context" },
  { id: "activity", label: "Activity" },
  { id: "files", label: "Files" },
  { id: "memory", label: "Memory" },
  { id: "tools", label: "Tools" },
];

export function Inspector({
  selectedWorkspaceFile,
  workspaceRefreshKey,
}: {
  selectedWorkspaceFile?: string | null;
  workspaceRefreshKey?: number;
}) {
  const [activeTab, setActiveTab] = useState<InspectorTab>("context");
  const [activityItems, setActivityItems] = useState<ActivityItem[]>([]);

  useEffect(() => {
    return subscribeRuntimeEvents((event) => {
      if (!isActivityEvent(event)) return;
      const item = activityItemFromEvent(event);
      setActivityItems((current) => [item, ...current].slice(0, 50));
    });
  }, []);

  return (
    <aside className="inspector">
      <div className="inspector-tabs">
        {TABS.map((tab) => (
          <button
            key={tab.id}
            className={`inspector-tab ${activeTab === tab.id ? "inspector-tab--active" : ""}`}
            onClick={() => setActiveTab(tab.id)}
          >
            {tab.label}
          </button>
        ))}
      </div>

      <div className="inspector-content">
        {activeTab === "context" && <ContextPanel refreshKey={workspaceRefreshKey} />}
        {activeTab === "activity" && <ActivityPanel items={activityItems} />}
        {activeTab === "files" && (
          <FilesPanel
            selectedFilePath={selectedWorkspaceFile ?? null}
            refreshKey={workspaceRefreshKey}
          />
        )}
        {activeTab === "memory" && <MemoryPanel />}
        {activeTab === "tools" && <ToolsPanel />}
      </div>
    </aside>
  );
}

function ContextPanel({
  refreshKey,
}: {
  refreshKey?: number;
}) {
  const status = useContextStatus();
  const bootstrap = useContextBootstrap();
  const skills = useContextSkills();
  const [selectedBootstrap, setSelectedBootstrap] = useState<string | null>(null);
  const bootstrapPreview = useContextBootstrapPreview(selectedBootstrap);
  const loading = status.loading || bootstrap.loading || skills.loading;
  const error = status.error || bootstrap.error || skills.error;
  const budget = status.data?.budget;
  const budgetPercent = budget?.context_limit_chars
    ? Math.min(100, Math.round((budget.estimated_usage / budget.context_limit_chars) * 100))
    : 0;

  useEffect(() => {
    if (!refreshKey) return;
    status.refresh();
    bootstrap.refresh();
    skills.refresh();
    bootstrapPreview.refresh();
  }, [refreshKey]);

  return (
    <div className="inspector-section">
      {loading && <div className="inspector-placeholder">Loading…</div>}
      {error && <div className="inspector-placeholder inspector-placeholder--error">Error: {error}</div>}

      {status.data && (
        <>
          <h3 className="inspector-heading">Summary</h3>
          <ul className="inspector-list">
            <li className="context-summary-item">
              <span>Memory</span>
              <span>{status.data.memory.ltm_items} facts · {status.data.memory.lessons_count} lessons</span>
            </li>
            <li className="context-summary-item">
              <span>Pinned</span>
              <span>{status.data.pinned_files.count} files</span>
            </li>
            <li className="context-summary-item">
              <span>Bootstrap</span>
              <span>{status.data.bootstrap_files.filter((file) => file.exists).length}/{status.data.bootstrap_files.length}</span>
            </li>
            <li className="context-summary-item">
              <span>Skills</span>
              <span>{status.data.skills.length}</span>
            </li>
          </ul>
        </>
      )}

      <h3 className="inspector-heading">Bootstrap</h3>
      {bootstrap.data && bootstrap.data.files.length > 0 ? (
        <ul className="inspector-list">
          {bootstrap.data.files.map((file) => (
            <li key={file.name} className="context-file-item">
              <button
                type="button"
                className={`context-file-button ${selectedBootstrap === file.name ? "context-file-button--active" : ""}`}
                onClick={() => setSelectedBootstrap(file.name)}
              >
                <span className="context-file-name">{file.name}</span>
                <span className="context-file-meta">
                  {bootstrapFileMeta(file)}
                </span>
              </button>
            </li>
          ))}
        </ul>
      ) : !loading && !error ? (
        <div className="inspector-placeholder">No bootstrap files</div>
      ) : null}

      <BootstrapPreview preview={bootstrapPreview} selectedName={selectedBootstrap} />

      <h3 className="inspector-heading">Skills</h3>
      {skills.data && skills.data.skills.length > 0 ? (
        <ul className="inspector-list">
          {skills.data.skills.slice(0, 12).map((skill, index) => (
            <li key={skillKey(skill, index)} className="context-skill-item">
              <span className="context-skill-name">{skillLabel(skill)}</span>
              {skill.description && <span className="context-skill-desc">{skill.description}</span>}
            </li>
          ))}
        </ul>
      ) : !loading && !error ? (
        <div className="inspector-placeholder">No skills</div>
      ) : null}

      <h3 className="inspector-heading">Context Budget</h3>
      <div className="inspector-meter">
        <div className="inspector-meter-bar" style={{ width: `${budgetPercent}%` }} />
        <span className="inspector-meter-label">
          {formatCompactNumber(budget?.estimated_usage ?? 0)} / {formatCompactNumber(budget?.context_limit_chars ?? 0)} chars
        </span>
      </div>
    </div>
  );
}

function BootstrapPreview({
  preview,
  selectedName,
}: {
  preview: {
    data: {
      name: string;
      exists: boolean;
      source: string;
      size: number;
      content: string | null;
      truncated: boolean;
    } | null;
    loading: boolean;
    error: string | null;
  };
  selectedName: string | null;
}) {
  if (!selectedName) return <div className="inspector-placeholder">Select a bootstrap file</div>;
  if (preview.loading) return <div className="inspector-placeholder">Loading bootstrap…</div>;
  if (preview.error) return <div className="inspector-placeholder inspector-placeholder--error">Error: {preview.error}</div>;
  if (!preview.data) return null;
  if (!preview.data.exists) return <div className="inspector-placeholder">{selectedName} is not present</div>;

  return (
    <div className="bootstrap-preview">
      <span className="bootstrap-preview-meta">
        {preview.data.source} · {formatBytes(preview.data.size)}
        {preview.data.truncated ? " · truncated" : ""}
      </span>
      <pre>{preview.data.content ?? ""}</pre>
    </div>
  );
}

interface ActivityItem {
  id: string;
  time: string;
  type: RuntimeEvent["type"];
  summary: string;
  pending: boolean;
}

function isActivityEvent(event: RuntimeEvent): boolean {
  return [
    "RunStarted",
    "RunCompleted",
    "RunCancelled",
    "ToolCallStarted",
    "ToolProgress",
    "ToolResult",
    "ApprovalRequested",
    "ApprovalResolved",
    "Error",
  ].includes(event.type);
}

function activityItemFromEvent(event: RuntimeEvent): ActivityItem {
  return {
    id: `${Date.now()}-${Math.random().toString(36).slice(2)}`,
    time: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }),
    type: event.type,
    summary: activitySummary(event),
    pending: event.type === "ApprovalRequested" || (event.type === "ToolResult" && event.is_error === true),
  };
}

function activitySummary(event: RuntimeEvent): string {
  switch (event.type) {
    case "RunStarted":
      return `${event.session_key} · ${event.preview ?? event.execution_id}`;
    case "RunCompleted":
      return `${event.session_key} · ${event.response_preview ?? event.execution_id}`;
    case "RunCancelled":
      return `${event.session_key} · ${event.reason ?? "cancelled"}`;
    case "ToolCallStarted":
      return `${event.tool_name}${event.tool_call_id ? ` · ${event.tool_call_id}` : ""}`;
    case "ToolProgress":
      return `${event.tool_name} · ${event.message ?? `${event.elapsed_seconds ?? 0}s elapsed`}`;
    case "ToolResult":
      return `${event.tool_name} · ${event.is_error ? "error" : "ok"}${event.preview ? ` · ${event.preview}` : ""}`;
    case "ApprovalRequested":
      return `${event.tool_name} · ${event.pattern_description ?? event.command_preview ?? event.approval_id}`;
    case "ApprovalResolved":
      return `${event.decision} · ${event.approval_id}`;
    case "Error":
      return `${event.source ?? "runtime"} · ${event.message}`;
    default:
      return event.type;
  }
}

function bootstrapFileMeta(file: ContextBootstrapFile): string {
  const source = file.exists ? file.source : "missing";
  const override = file.has_workspace_override ? " · override" : "";
  return `${source}${override} · ${formatBytes(file.size)}`;
}

function ActivityPanel({ items }: { items: ActivityItem[] }) {
  return (
    <div className="inspector-section">
      <h3 className="inspector-heading">Recent Activity</h3>
      {items.length === 0 ? (
        <div className="inspector-placeholder">No runtime activity yet</div>
      ) : (
        <ul className="inspector-list">
          {items.map((item) => (
            <li key={item.id} className={`activity-item ${item.pending ? "activity-item--pending" : ""}`}>
              <span className="activity-time">{item.time}</span>
              <span className="activity-action">{item.type}</span>
              <span className="activity-target" title={item.summary}>{item.summary}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function FilesPanel({
  selectedFilePath,
  refreshKey,
}: {
  selectedFilePath: string | null;
  refreshKey?: number;
}) {
  const pinned = useWorkspacePinned();
  const recent = useWorkspaceRecent(10);
  const preview = useWorkspacePreview(selectedFilePath);

  useEffect(() => {
    if (!refreshKey) return;
    pinned.refresh();
    recent.refresh();
    preview.refresh();
  }, [refreshKey]);

  return (
    <div className="inspector-section">
      <h3 className="inspector-heading">Selected File</h3>
      <InspectorPreview preview={preview} />

      <h3 className="inspector-heading">Pinned Files</h3>
      <InspectorFileList
        files={pinned.data?.files ?? []}
        loading={pinned.loading}
        error={pinned.error}
        emptyText="No pinned files"
      />

      <h3 className="inspector-heading">Recent Files</h3>
      <InspectorFileList
        files={recent.data?.files ?? []}
        loading={recent.loading}
        error={recent.error}
        emptyText="No recent files"
      />
    </div>
  );
}

function InspectorFileList({
  files,
  loading,
  error,
  emptyText,
}: {
  files: WorkspaceFileInfo[];
  loading: boolean;
  error: string | null;
  emptyText: string;
}) {
  if (loading) return <div className="inspector-placeholder">Loading…</div>;
  if (error) return <div className="inspector-placeholder inspector-placeholder--error">Error: {error}</div>;
  if (files.length === 0) return <div className="inspector-placeholder">{emptyText}</div>;

  return (
    <ul className="inspector-list">
      {files.map((file) => (
        <li key={file.path} className="file-item">
          <span className="file-path" title={file.path}>{file.path}</span>
          <span className="file-size">{formatBytes(file.size)}</span>
        </li>
      ))}
    </ul>
  );
}

function InspectorPreview({
  preview,
}: {
  preview: {
    data: WorkspacePreviewResult | null;
    loading: boolean;
    error: string | null;
  };
}) {
  if (preview.loading) return <div className="inspector-placeholder">Loading preview…</div>;
  if (preview.error) return <div className="inspector-placeholder inspector-placeholder--error">Error: {preview.error}</div>;
  if (!preview.data) return <div className="inspector-placeholder">No file selected</div>;
  if (!preview.data.exists) return <div className="inspector-placeholder">File does not exist</div>;

  const meta = preview.data.is_dir
    ? "directory"
    : preview.data.is_binary
      ? `${formatBytes(preview.data.size ?? 0)} · binary`
      : `${formatBytes(preview.data.size ?? 0)}${preview.data.truncated ? " · truncated" : ""}`;

  return (
    <div className="inspector-file-preview">
      <span className="inspector-file-preview-path" title={preview.data.path}>{preview.data.path}</span>
      <span className="inspector-file-preview-meta">{meta}</span>
      {!preview.data.is_dir && !preview.data.is_binary && preview.data.content && (
        <pre>{preview.data.content.slice(0, 800)}</pre>
      )}
    </div>
  );
}

function MemoryPanel() {
  const { data, loading, error, refresh } = useMemoryStatus();
  const snapshot = useMemorySnapshot(8);
  const lessons = useMemoryLessons(true, 8);

  const refreshMemory = () => {
    refresh();
    snapshot.refresh();
    lessons.refresh();
  };

  return (
    <div className="inspector-section">
      <div className="inspector-heading-row">
        <h3 className="inspector-heading">Memory</h3>
        <button type="button" onClick={refreshMemory}>Refresh</button>
      </div>
      {loading && <div className="inspector-placeholder">Loading…</div>}
      {error && <div className="inspector-placeholder inspector-placeholder--error">Error: {error}</div>}
      {data && (
        <ul className="inspector-list">
          <li className="memory-item">
            <span className="memory-text">Long-term items</span>
            <span className="memory-count">{data.ltm_items}</span>
          </li>
          <li className="memory-item">
            <span className="memory-text">Lessons</span>
            <span className="memory-count">{data.lessons_count}</span>
          </li>
          <li className="memory-item">
            <span className="memory-text">Snapshot</span>
            <span className="memory-count">{data.snapshot_exists ? "exists" : "none"}</span>
          </li>
          <li className="memory-item">
            <span className="memory-text">Self-improvement</span>
            <span className="memory-count">{data.self_improvement_enabled ? "on" : "off"}</span>
          </li>
        </ul>
      )}

      <h3 className="inspector-heading">Lessons</h3>
      {lessons.loading && <div className="inspector-placeholder">Loading…</div>}
      {lessons.error && <div className="inspector-placeholder inspector-placeholder--error">Error: {lessons.error}</div>}
      {lessons.data && lessons.data.lessons.length === 0 && (
        <div className="inspector-placeholder">No lessons</div>
      )}
      {lessons.data && lessons.data.lessons.length > 0 && (
        <ul className="inspector-list">
          {lessons.data.lessons.map((lesson) => (
            <li key={lesson.id} className="memory-detail-item">
              <span className="memory-text">{lesson.trigger}</span>
              <span className="memory-count">{lesson.enabled === false ? "off" : "on"}</span>
            </li>
          ))}
        </ul>
      )}

      <h3 className="inspector-heading">Snapshot</h3>
      {snapshot.loading && <div className="inspector-placeholder">Loading…</div>}
      {snapshot.error && <div className="inspector-placeholder inspector-placeholder--error">Error: {snapshot.error}</div>}
      {snapshot.data && snapshot.data.items.length === 0 && (
        <div className="inspector-placeholder">No snapshot items</div>
      )}
      {snapshot.data && snapshot.data.items.length > 0 && (
        <ul className="inspector-list">
          {snapshot.data.items.map((item) => (
            <li key={item.id} className="memory-detail-item">
              <span className="memory-text">{item.text}</span>
              <span className="memory-count">{item.hits ?? 0}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function ToolsPanel() {
  const toolData = useToolList();
  const mcpData = useMcpStatus();
  const loading = toolData.loading || mcpData.loading;
  const error = toolData.error || mcpData.error;

  const refreshTools = () => {
    toolData.refresh();
    mcpData.refresh();
  };

  return (
    <div className="inspector-section">
      <div className="inspector-heading-row">
        <h3 className="inspector-heading">Available Tools</h3>
        <button type="button" onClick={refreshTools}>Refresh</button>
      </div>
      {loading && <div className="inspector-placeholder">Loading…</div>}
      {error && (
        <div className="inspector-placeholder inspector-placeholder--error">
          Error: {error}
        </div>
      )}
      {toolData.data && (
        <ul className="inspector-list">
          {toolData.data.tools.map((t) => (
            <li key={t.name} className="tool-item">
              <span className="tool-name">{t.name}</span>
              <span className="tool-source">builtin</span>
            </li>
          ))}
        </ul>
      )}
      {mcpData.data && Object.keys(mcpData.data.servers).length > 0 && (
        <>
          <h3 className="inspector-heading">MCP Servers</h3>
          <ul className="inspector-list">
            {Object.entries(mcpData.data.servers).map(([name, status]) => (
              <li key={name} className="tool-item">
                <span className="tool-name">{name}</span>
                <span className={`tool-source ${status.connected ? "tool-source--connected" : "tool-source--disconnected"}`}>
                  {status.connected ? "connected" : "disconnected"}
                </span>
              </li>
            ))}
          </ul>
        </>
      )}
      {mcpData.data && Object.keys(mcpData.data.servers).length === 0 && (
        <>
          <h3 className="inspector-heading">MCP Servers</h3>
          <div className="inspector-placeholder">
            No MCP servers configured. Status: {mcpData.data.connecting ? "connecting" : mcpData.data.connected ? "connected" : "disconnected"}
          </div>
        </>
      )}
      {mcpData.data && (
        <>
          <h3 className="inspector-heading">MCP Status</h3>
          <ul className="inspector-list">
            <li className="tool-item">
              <span className="tool-name">connected</span>
              <span className={`tool-source ${mcpData.data.connected ? "tool-source--connected" : "tool-source--disconnected"}`}>
                {mcpData.data.connected ? "yes" : "no"}
              </span>
            </li>
            <li className="tool-item">
              <span className="tool-name">connecting</span>
              <span className="tool-source">{mcpData.data.connecting ? "yes" : "no"}</span>
            </li>
            <li className="tool-item">
              <span className="tool-name">retry</span>
              <span className="tool-source">{mcpData.data.retry_after}s</span>
            </li>
          </ul>
        </>
      )}
    </div>
  );
}

function skillKey(skill: ContextSkill, index: number): string {
  return String(skill.name ?? skill.id ?? skill.path ?? index);
}

function skillLabel(skill: ContextSkill): string {
  return String(skill.name ?? skill.id ?? skill.path ?? "unnamed skill");
}

function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function formatCompactNumber(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${Math.round(value / 1_000)}k`;
  return String(value);
}
