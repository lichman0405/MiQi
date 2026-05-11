import { useState, useEffect, useMemo, useRef } from "react";
import type { RailTab } from "../App";
import {
  useToolList,
  useMemoryStatus,
  useMemorySnapshot,
  useMemoryLessons,
  useCronList,
  useHeartbeatStatus,
  useWorkspaceIndex,
  useWorkspacePinned,
  useWorkspacePreview,
  useWorkspaceRecent,
  useWorkspaceStatus,
  type SessionInfo,
} from "../lib/hooks";
import {
  archiveSession,
  deleteSession,
  listSessions,
  renameSession,
  searchSessions,
  unarchiveSession,
} from "../lib/chat-state";
import {
  openWorkspace,
  pinWorkspaceFile,
  unpinWorkspaceFile,
  type WorkspaceEntry,
  type WorkspaceFileInfo,
  type WorkspacePreviewResult,
} from "../lib/workspace-state";
import {
  addCronJob,
  appendTodayMemory,
  deleteCronJob,
  deleteMemoryLesson,
  deleteMemorySnapshotItem,
  learnMemoryLesson,
  rememberMemory,
  searchMemory,
  setMemoryLessonEnabled,
  updateCronJob,
  updateHeartbeat,
  updateMemory,
  type CronJob,
  type MemorySearchResult,
} from "../lib/ops-state";
import "./ListPanel.css";

interface ListPanelProps {
  activeTab: RailTab;
  sessionRefreshKey?: number;
  activeSessionKey?: string | null;
  sessionActionsDisabled?: boolean;
  onNewChat?: () => void;
  onSelectSession?: (key: string, title?: string) => void;
  onSessionRenamed?: (key: string, title: string) => void;
  onSessionRemoved?: (key: string) => void;
  activeWorkspaceFile?: string | null;
  onSelectWorkspaceFile?: (path: string | null) => void;
  onWorkspaceChanged?: () => void;
}

export function ListPanel({
  activeTab,
  sessionRefreshKey,
  activeSessionKey,
  sessionActionsDisabled = false,
  onNewChat,
  onSelectSession,
  onSessionRenamed,
  onSessionRemoved,
  activeWorkspaceFile,
  onSelectWorkspaceFile,
  onWorkspaceChanged,
}: ListPanelProps) {
  const [search, setSearch] = useState("");
  const [includeArchived, setIncludeArchived] = useState(false);

  return (
    <aside className="list-panel">
      <div className="list-panel-header">
        <h2 className="list-panel-title">{tabLabel(activeTab)}</h2>
        {activeTab === "chats" && (
          <button
            className="list-panel-action"
            title={sessionActionsDisabled ? "Run is active" : "New chat"}
            onClick={onNewChat}
            disabled={sessionActionsDisabled}
          >
            +
          </button>
        )}
      </div>

      {activeTab === "chats" && (
        <div className="list-panel-search">
          <input
            type="text"
            placeholder="Search sessions..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <label className="list-panel-toggle">
            <input
              type="checkbox"
              checked={includeArchived}
              onChange={(e) => setIncludeArchived(e.target.checked)}
            />
            <span>Archived</span>
          </label>
        </div>
      )}

      <div className="list-panel-content">
        {activeTab === "chats" && (
          <SessionList
            search={search}
            includeArchived={includeArchived}
            refreshKey={sessionRefreshKey}
            activeSessionKey={activeSessionKey}
            actionsDisabled={sessionActionsDisabled}
            onSelect={onSelectSession}
            onRenamed={onSessionRenamed}
            onRemoved={onSessionRemoved}
          />
        )}
        {activeTab === "files" && (
          <FileList
            activeFilePath={activeWorkspaceFile}
            onSelectFile={onSelectWorkspaceFile}
            onWorkspaceChanged={onWorkspaceChanged}
          />
        )}
        {activeTab === "tools" && <ToolList />}
        {activeTab === "memory" && <MemoryList />}
        {activeTab === "cron" && <CronListView />}
      </div>
    </aside>
  );
}

function tabLabel(tab: RailTab): string {
  const labels: Record<RailTab, string> = {
    chats: "Chats",
    files: "Files",
    tools: "Tools",
    memory: "Memory",
    cron: "Cron",
    settings: "Settings",
  };
  return labels[tab];
}

function SessionList({
  search,
  includeArchived,
  refreshKey,
  activeSessionKey,
  actionsDisabled,
  onSelect,
  onRenamed,
  onRemoved,
}: {
  search: string;
  includeArchived: boolean;
  refreshKey?: number;
  activeSessionKey?: string | null;
  actionsDisabled: boolean;
  onSelect?: (key: string, title?: string) => void;
  onRenamed?: (key: string, title: string) => void;
  onRemoved?: (key: string) => void;
}) {
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [operationError, setOperationError] = useState<string | null>(null);
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [renameDraft, setRenameDraft] = useState("");
  const requestSeq = useRef(0);

  useEffect(() => {
    const requestId = requestSeq.current + 1;
    requestSeq.current = requestId;
    const timer = setTimeout(() => {
      const query = search.trim();
      setLoading(true);
      setError(null);
      const fetch = query
        ? searchSessions(query, includeArchived)
        : listSessions(includeArchived);

      fetch
        .then((result) => {
          if (requestId !== requestSeq.current) return;
          setSessions(result.sessions);
          setLoading(false);
        })
        .catch((err: unknown) => {
          if (requestId !== requestSeq.current) return;
          setSessions([]);
          setLoading(false);
          setError(err instanceof Error ? err.message : String(err));
        });
    }, 180);

    return () => clearTimeout(timer);
  }, [search, includeArchived, refreshKey]);

  const refreshSessions = () => {
    const requestId = requestSeq.current + 1;
    requestSeq.current = requestId;
    const query = search.trim();
    setLoading(true);
    setError(null);
    const fetch = query ? searchSessions(query, includeArchived) : listSessions(includeArchived);
    fetch
      .then((result) => {
        if (requestId !== requestSeq.current) return;
        setSessions(result.sessions);
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (requestId !== requestSeq.current) return;
        setSessions([]);
        setLoading(false);
        setError(err instanceof Error ? err.message : String(err));
      });
  };

  const beginRename = (session: SessionInfo) => {
    setEditingKey(session.key);
    setRenameDraft(session.title);
    setOperationError(null);
  };

  const commitRename = async (session: SessionInfo) => {
    const title = renameDraft.trim();
    if (!title || title === session.title) {
      setEditingKey(null);
      setRenameDraft("");
      return;
    }
    try {
      await renameSession(session.key, title);
      setEditingKey(null);
      setRenameDraft("");
      onRenamed?.(session.key, title);
      refreshSessions();
    } catch (err) {
      setOperationError(err instanceof Error ? err.message : String(err));
    }
  };

  const archive = async (session: SessionInfo) => {
    try {
      await archiveSession(session.key);
      onRemoved?.(session.key);
      refreshSessions();
    } catch (err) {
      setOperationError(err instanceof Error ? err.message : String(err));
    }
  };

  const unarchive = async (session: SessionInfo) => {
    try {
      await unarchiveSession(session.key);
      refreshSessions();
    } catch (err) {
      setOperationError(err instanceof Error ? err.message : String(err));
    }
  };

  const remove = async (session: SessionInfo) => {
    const confirmed = window.confirm(`Delete session "${session.title}"?`);
    if (!confirmed) return;
    try {
      await deleteSession(session.key);
      onRemoved?.(session.key);
      refreshSessions();
    } catch (err) {
      setOperationError(err instanceof Error ? err.message : String(err));
    }
  };

  if (loading) return <div className="list-empty">Loading…</div>;
  if (error) return <div className="list-empty list-empty--error">Error: {error}</div>;
  if (sessions.length === 0) return <div className="list-empty">No sessions</div>;

  return (
    <>
      {operationError && <div className="list-operation-error">{operationError}</div>}
      <ul className="list-items">
        {sessions.map((s: SessionInfo) => (
          <li
            key={s.key}
            className={`list-item list-item--selectable ${s.key === activeSessionKey ? "list-item--active" : ""} ${s.archived ? "list-item--archived" : ""}`}
            onClick={() => {
              if (!actionsDisabled) onSelect?.(s.key, s.title);
            }}
          >
            {editingKey === s.key ? (
              <div className="list-rename" onClick={(e) => e.stopPropagation()}>
                <input
                  value={renameDraft}
                  onChange={(e) => setRenameDraft(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") void commitRename(s);
                    if (e.key === "Escape") {
                      setEditingKey(null);
                      setRenameDraft("");
                    }
                  }}
                  autoFocus
                />
                <button type="button" onClick={() => void commitRename(s)}>Save</button>
                <button type="button" onClick={() => { setEditingKey(null); setRenameDraft(""); }}>Cancel</button>
              </div>
            ) : (
              <>
                <div className="list-item-main">
                  <span className="list-item-title">{s.title}</span>
                  <span className="list-item-meta">
                    {s.message_count} msgs{s.archived ? " · archived" : ""}
                  </span>
                </div>
                <div className="list-item-actions" onClick={(e) => e.stopPropagation()}>
                  <button type="button" onClick={() => beginRename(s)} disabled={actionsDisabled}>Rename</button>
                  {s.archived ? (
                    <button type="button" onClick={() => void unarchive(s)} disabled={actionsDisabled}>Unarchive</button>
                  ) : (
                    <button type="button" onClick={() => void archive(s)} disabled={actionsDisabled}>Archive</button>
                  )}
                  <button type="button" onClick={() => void remove(s)} disabled={actionsDisabled}>Delete</button>
                </div>
              </>
            )}
          </li>
        ))}
      </ul>
    </>
  );
}

function FileList({
  activeFilePath,
  onSelectFile,
  onWorkspaceChanged,
}: {
  activeFilePath?: string | null;
  onSelectFile?: (path: string | null) => void;
  onWorkspaceChanged?: () => void;
}) {
  const status = useWorkspaceStatus();
  const index = useWorkspaceIndex();
  const pinned = useWorkspacePinned();
  const recent = useWorkspaceRecent(10);
  const preview = useWorkspacePreview(activeFilePath ?? null);
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const [workspaceDraft, setWorkspaceDraft] = useState("");
  const [operationError, setOperationError] = useState<string | null>(null);
  const [openingWorkspace, setOpeningWorkspace] = useState(false);

  useEffect(() => {
    if (status.data?.project_root) setWorkspaceDraft(status.data.project_root);
  }, [status.data?.project_root]);

  const pinnedPaths = useMemo(
    () => new Set((pinned.data?.files ?? []).map((file) => file.path)),
    [pinned.data],
  );

  const visibleEntries = useMemo(
    () => (index.data?.entries ?? []).filter((entry) => isEntryVisible(entry, expanded)),
    [index.data, expanded],
  );

  const refreshWorkspaceLists = () => {
    index.refresh();
    pinned.refresh();
    recent.refresh();
    status.refresh();
  };

  const toggleDirectory = (path: string) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  };

  const selectFile = (path: string) => {
    setOperationError(null);
    onSelectFile?.(path);
  };

  const openWorkspacePath = async () => {
    const path = workspaceDraft.trim();
    if (!path || path === status.data?.project_root) return;
    setOperationError(null);
    setOpeningWorkspace(true);
    try {
      await openWorkspace(path);
      setExpanded(new Set());
      onSelectFile?.(null);
      refreshWorkspaceLists();
      onWorkspaceChanged?.();
    } catch (err) {
      setOperationError(err instanceof Error ? err.message : String(err));
    } finally {
      setOpeningWorkspace(false);
    }
  };

  const togglePin = async (path: string, shouldPin: boolean) => {
    setOperationError(null);
    try {
      if (shouldPin) await pinWorkspaceFile(path);
      else await unpinWorkspaceFile(path);
      refreshWorkspaceLists();
      onWorkspaceChanged?.();
    } catch (err) {
      setOperationError(err instanceof Error ? err.message : String(err));
    }
  };

  if (status.loading || index.loading) return <div className="list-empty">Loading workspace…</div>;
  if (status.error || index.error) {
    return (
      <div className="list-empty list-empty--error">
        Error: {status.error || index.error}
      </div>
    );
  }

  return (
    <div className="workspace-browser">
      {status.data && (
        <div className="workspace-summary">
          <span className="workspace-root" title={status.data.project_root}>
            {status.data.project_root}
          </span>
          <span className="workspace-meta">
            {status.data.restrict_to_workspace ? "restricted" : "unrestricted"} · {status.data.pinned_count} pinned · {status.data.recent_count} recent
          </span>
          <div className="workspace-open-row">
            <input
              type="text"
              value={workspaceDraft}
              onChange={(e) => setWorkspaceDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") void openWorkspacePath();
              }}
              aria-label="Workspace path"
            />
            <button
              type="button"
              onClick={() => void openWorkspacePath()}
              disabled={openingWorkspace || !workspaceDraft.trim()}
            >
              {openingWorkspace ? "Opening…" : "Open"}
            </button>
          </div>
        </div>
      )}

      {operationError && <div className="list-operation-error">{operationError}</div>}

      <FileQuickList
        title="Pinned"
        files={pinned.data?.files ?? []}
        loading={pinned.loading}
        error={pinned.error}
        emptyText="No pinned files"
        activeFilePath={activeFilePath}
        onSelectFile={selectFile}
        onUnpin={(path) => void togglePin(path, false)}
      />

      <FileQuickList
        title="Recent"
        files={recent.data?.files ?? []}
        loading={recent.loading}
        error={recent.error}
        emptyText="No recent files"
        activeFilePath={activeFilePath}
        onSelectFile={selectFile}
      />

      <div className="workspace-section-heading">Files</div>
      {index.data && index.data.entries.length === 0 && (
        <div className="list-empty">No files</div>
      )}
      {index.data && index.data.entries.length > 0 && (
        <ul className="file-tree">
          {visibleEntries.map((entry) => {
            const depth = fileDepth(entry.path);
            const isPinned = pinnedPaths.has(entry.path);
            return (
              <li
                key={entry.path}
                className={`file-tree-row ${entry.path === activeFilePath ? "file-tree-row--active" : ""}`}
                style={{ paddingLeft: `${8 + depth * 12}px` }}
              >
                <button
                  type="button"
                  className="file-tree-main"
                  onClick={() => {
                    if (entry.is_dir) toggleDirectory(entry.path);
                    else selectFile(entry.path);
                  }}
                  title={entry.path}
                >
                  <span className="file-tree-chevron">
                    {entry.is_dir ? (expanded.has(entry.path) ? "▾" : "▸") : " "}
                  </span>
                  <span className="file-tree-icon">{entry.is_dir ? "dir" : "file"}</span>
                  <span className="file-tree-name">{entry.name}</span>
                </button>
                {!entry.is_dir && (
                  <button
                    type="button"
                    className="file-tree-pin"
                    title={isPinned ? "Unpin file" : "Pin file"}
                    onClick={(e) => {
                      e.stopPropagation();
                      void togglePin(entry.path, !isPinned);
                    }}
                  >
                    {isPinned ? "Pinned" : "Pin"}
                  </button>
                )}
              </li>
            );
          })}
        </ul>
      )}

      <FilePreview preview={preview} />
    </div>
  );
}

function FileQuickList({
  title,
  files,
  loading,
  error,
  emptyText,
  activeFilePath,
  onSelectFile,
  onUnpin,
}: {
  title: string;
  files: WorkspaceFileInfo[];
  loading: boolean;
  error: string | null;
  emptyText: string;
  activeFilePath?: string | null;
  onSelectFile: (path: string) => void;
  onUnpin?: (path: string) => void;
}) {
  return (
    <div className="workspace-quick-list">
      <div className="workspace-section-heading">{title}</div>
      {loading && <div className="workspace-small-empty">Loading…</div>}
      {error && <div className="workspace-small-empty workspace-small-empty--error">Error: {error}</div>}
      {!loading && !error && files.length === 0 && (
        <div className="workspace-small-empty">{emptyText}</div>
      )}
      {!loading && !error && files.length > 0 && (
        <ul className="workspace-file-list">
          {files.map((file) => (
            <li
              key={file.path}
              className={`workspace-file-chip ${file.path === activeFilePath ? "workspace-file-chip--active" : ""}`}
            >
              <button
                type="button"
                className="workspace-file-chip-main"
                title={file.path}
                onClick={() => onSelectFile(file.path)}
              >
                <span className="workspace-file-path">{file.path}</span>
                <span className="workspace-file-size">{formatBytes(file.size)}</span>
              </button>
              {onUnpin && (
                <button
                  type="button"
                  className="workspace-file-chip-action"
                  title="Unpin file"
                  onClick={() => onUnpin(file.path)}
                >
                  Unpin
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function FilePreview({
  preview,
}: {
  preview: {
    data: WorkspacePreviewResult | null;
    loading: boolean;
    error: string | null;
  };
}) {
  if (preview.loading) return <div className="file-preview">Loading preview…</div>;
  if (preview.error) return <div className="file-preview file-preview--error">Error: {preview.error}</div>;
  if (!preview.data) return <div className="file-preview">Select a file to preview.</div>;
  if (!preview.data.exists) return <div className="file-preview">File does not exist.</div>;
  if (preview.data.is_dir) return <div className="file-preview">Directory selected.</div>;
  if (preview.data.is_binary) {
    return (
      <div className="file-preview">
        <span className="file-preview-title">{preview.data.path}</span>
        <span className="file-preview-meta">{formatBytes(preview.data.size ?? 0)} · binary</span>
      </div>
    );
  }

  return (
    <div className="file-preview">
      <span className="file-preview-title">{preview.data.path}</span>
      <span className="file-preview-meta">
        {formatBytes(preview.data.size ?? 0)}{preview.data.truncated ? " · truncated" : ""}
      </span>
      <pre>{preview.data.content || ""}</pre>
    </div>
  );
}

function isEntryVisible(entry: WorkspaceEntry, expanded: Set<string>): boolean {
  const parts = entry.path.split("/");
  for (let i = 1; i < parts.length; i += 1) {
    const ancestor = parts.slice(0, i).join("/");
    if (!expanded.has(ancestor)) return false;
  }
  return true;
}

function fileDepth(path: string): number {
  return Math.max(0, path.split("/").length - 1);
}

function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function ToolList() {
  const { data, loading, error } = useToolList();

  if (loading) return <div className="list-empty">Loading…</div>;
  if (error) return <div className="list-empty list-empty--error">Error: {error}</div>;
  if (!data || data.tools.length === 0) return <div className="list-empty">No tools</div>;

  return (
    <ul className="list-items">
      {data.tools.map((t) => (
        <li key={t.name} className="list-item">
          <span className="list-item-title">{t.name}</span>
        </li>
      ))}
    </ul>
  );
}

function MemoryList() {
  const status = useMemoryStatus();
  const snapshot = useMemorySnapshot(20);
  const lessons = useMemoryLessons(true, 20);
  const [query, setQuery] = useState("");
  const [searchResults, setSearchResults] = useState<MemorySearchResult[]>([]);
  const [searchLoading, setSearchLoading] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [rememberText, setRememberText] = useState("");
  const [todayText, setTodayText] = useState("");
  const [lessonTrigger, setLessonTrigger] = useState("");
  const [lessonBetterAction, setLessonBetterAction] = useState("");
  const [operationError, setOperationError] = useState<string | null>(null);
  const [operationMessage, setOperationMessage] = useState<string | null>(null);
  const searchSeq = useRef(0);

  const refreshMemory = () => {
    status.refresh();
    snapshot.refresh();
    lessons.refresh();
  };

  useEffect(() => {
    const trimmed = query.trim();
    const requestId = searchSeq.current + 1;
    searchSeq.current = requestId;

    if (!trimmed) {
      setSearchResults([]);
      setSearchLoading(false);
      setSearchError(null);
      return;
    }

    const timer = setTimeout(() => {
      setSearchLoading(true);
      setSearchError(null);
      searchMemory(trimmed, 20)
        .then((result) => {
          if (requestId !== searchSeq.current) return;
          setSearchResults(result.results);
          setSearchLoading(false);
        })
        .catch((err: unknown) => {
          if (requestId !== searchSeq.current) return;
          setSearchResults([]);
          setSearchLoading(false);
          setSearchError(err instanceof Error ? err.message : String(err));
        });
    }, 180);

    return () => clearTimeout(timer);
  }, [query]);

  const runMemoryAction = async (action: () => Promise<unknown>, successMessage: string) => {
    setOperationError(null);
    setOperationMessage(null);
    try {
      await action();
      setOperationMessage(successMessage);
      refreshMemory();
      if (query.trim()) {
        const result = await searchMemory(query.trim(), 20);
        setSearchResults(result.results);
      }
    } catch (err) {
      setOperationError(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <div className="ops-panel">
      <div className="ops-section">
        <div className="ops-section-header">
          <span>Memory</span>
          <button type="button" onClick={refreshMemory}>Refresh</button>
        </div>
        {status.loading && <div className="workspace-small-empty">Loading…</div>}
        {status.error && <div className="workspace-small-empty workspace-small-empty--error">Error: {status.error}</div>}
        {status.data && (
          <div className="ops-stats">
            <span>{status.data.ltm_items} facts</span>
            <span>{status.data.lessons_count} lessons</span>
            <span>{status.data.snapshot_exists ? "snapshot" : "no snapshot"}</span>
          </div>
        )}
        {operationError && <div className="list-operation-error">{operationError}</div>}
        {operationMessage && <div className="ops-success">{operationMessage}</div>}
      </div>

      <div className="ops-section">
        <div className="ops-section-heading">Search</div>
        <input
          className="ops-input"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search memory..."
        />
        {searchLoading && <div className="workspace-small-empty">Searching…</div>}
        {searchError && <div className="workspace-small-empty workspace-small-empty--error">Error: {searchError}</div>}
        {!searchLoading && query.trim() && searchResults.length === 0 && !searchError && (
          <div className="workspace-small-empty">No matches</div>
        )}
        {searchResults.length > 0 && (
          <ul className="ops-list">
            {searchResults.map((result, index) => (
              <li key={`${result.source}-${result.id ?? result.date ?? index}`} className="ops-list-item">
                <span className="ops-list-title">{memoryResultTitle(result)}</span>
                <span className="ops-list-meta">{result.source}</span>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="ops-section">
        <div className="ops-section-heading">Remember This</div>
        <textarea
          className="ops-textarea"
          value={rememberText}
          onChange={(e) => setRememberText(e.target.value)}
          placeholder="Long-term memory..."
          rows={3}
        />
        <div className="ops-actions">
          <button
            type="button"
            disabled={!rememberText.trim()}
            onClick={() => {
              const text = rememberText.trim();
              void runMemoryAction(
                async () => {
                  await rememberMemory(text);
                  setRememberText("");
                },
                "Remembered",
              );
            }}
          >
            Remember
          </button>
          <button
            type="button"
            disabled={!rememberText.trim()}
            onClick={() => {
              const text = rememberText.trim();
              void runMemoryAction(
                async () => {
                  await updateMemory({ text, action: "remember" });
                  setRememberText("");
                },
                "Memory updated",
              );
            }}
          >
            Update
          </button>
        </div>
      </div>

      <div className="ops-section">
        <div className="ops-section-heading">Append Today</div>
        <textarea
          className="ops-textarea"
          value={todayText}
          onChange={(e) => setTodayText(e.target.value)}
          placeholder="Daily note..."
          rows={3}
        />
        <div className="ops-actions">
          <button
            type="button"
            disabled={!todayText.trim()}
            onClick={() => {
              const text = todayText.trim();
              void runMemoryAction(
                async () => {
                  await appendTodayMemory(text);
                  setTodayText("");
                },
                "Appended",
              );
            }}
          >
            Append
          </button>
        </div>
      </div>

      <div className="ops-section">
        <div className="ops-section-heading">Learn Lesson</div>
        <input
          className="ops-input"
          value={lessonTrigger}
          onChange={(e) => setLessonTrigger(e.target.value)}
          placeholder="Trigger or mistake..."
        />
        <textarea
          className="ops-textarea"
          value={lessonBetterAction}
          onChange={(e) => setLessonBetterAction(e.target.value)}
          placeholder="Better action..."
          rows={2}
        />
        <div className="ops-actions">
          <button
            type="button"
            disabled={!lessonTrigger.trim() || !lessonBetterAction.trim()}
            onClick={() => {
              const trigger = lessonTrigger.trim();
              const betterAction = lessonBetterAction.trim();
              void runMemoryAction(
                async () => {
                  await learnMemoryLesson({ trigger, betterAction });
                  setLessonTrigger("");
                  setLessonBetterAction("");
                },
                "Lesson saved",
              );
            }}
          >
            Save lesson
          </button>
        </div>
      </div>

      <div className="ops-section">
        <div className="ops-section-heading">Lessons</div>
        {lessons.loading && <div className="workspace-small-empty">Loading…</div>}
        {lessons.error && <div className="workspace-small-empty workspace-small-empty--error">Error: {lessons.error}</div>}
        {lessons.data && lessons.data.lessons.length === 0 && <div className="workspace-small-empty">No lessons</div>}
        {lessons.data && lessons.data.lessons.length > 0 && (
          <ul className="ops-list">
            {lessons.data.lessons.map((lesson) => (
              <li key={lesson.id} className="ops-list-item ops-list-item--stacked">
                <span className="ops-list-title">{lesson.trigger}</span>
                <span className="ops-list-body">{lesson.better_action}</span>
                <div className="ops-actions">
                  <button
                    type="button"
                    onClick={() => void runMemoryAction(
                      () => setMemoryLessonEnabled(lesson.id, !lesson.enabled),
                      lesson.enabled === false ? "Lesson enabled" : "Lesson disabled",
                    )}
                  >
                    {lesson.enabled === false ? "Enable" : "Disable"}
                  </button>
                  <button
                    type="button"
                    onClick={() => void runMemoryAction(
                      () => deleteMemoryLesson(lesson.id),
                      "Lesson deleted",
                    )}
                  >
                    Delete
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="ops-section">
        <div className="ops-section-heading">Snapshot</div>
        {snapshot.loading && <div className="workspace-small-empty">Loading…</div>}
        {snapshot.error && <div className="workspace-small-empty workspace-small-empty--error">Error: {snapshot.error}</div>}
        {snapshot.data && snapshot.data.items.length === 0 && <div className="workspace-small-empty">No snapshot items</div>}
        {snapshot.data && snapshot.data.items.length > 0 && (
          <ul className="ops-list">
            {snapshot.data.items.map((item) => (
              <li key={item.id} className="ops-list-item ops-list-item--stacked">
                <span className="ops-list-title">{item.text}</span>
                <span className="ops-list-meta">
                  {item.source ?? "memory"} · {item.hits ?? 0} hits
                </span>
                <div className="ops-actions">
                  <button
                    type="button"
                    onClick={() => void runMemoryAction(
                      () => deleteMemorySnapshotItem(item.id),
                      "Snapshot item deleted",
                    )}
                  >
                    Delete
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function CronListView() {
  const cron = useCronList();
  const heartbeat = useHeartbeatStatus();
  const [name, setName] = useState("");
  const [message, setMessage] = useState("");
  const [scheduleKind, setScheduleKind] = useState<"every" | "at">("every");
  const [everyMinutes, setEveryMinutes] = useState("60");
  const [atLocal, setAtLocal] = useState("");
  const [heartbeatInterval, setHeartbeatInterval] = useState("");
  const [operationError, setOperationError] = useState<string | null>(null);
  const [operationMessage, setOperationMessage] = useState<string | null>(null);

  useEffect(() => {
    if (heartbeat.data) setHeartbeatInterval(String(heartbeat.data.interval_seconds));
  }, [heartbeat.data]);

  const refreshCron = () => {
    cron.refresh();
    heartbeat.refresh();
  };

  const runCronAction = async (action: () => Promise<unknown>, successMessage: string) => {
    setOperationError(null);
    setOperationMessage(null);
    try {
      await action();
      setOperationMessage(successMessage);
      refreshCron();
    } catch (err) {
      setOperationError(err instanceof Error ? err.message : String(err));
    }
  };

  const submitCronJob = () => {
    const trimmedName = name.trim();
    const trimmedMessage = message.trim();
    if (!trimmedName || !trimmedMessage) return;
    const schedule = scheduleKind === "every"
      ? { kind: "every" as const, every_ms: Math.max(1, Number(everyMinutes) || 1) * 60_000 }
      : { kind: "at" as const, at_ms: new Date(atLocal).getTime() };
    if (scheduleKind === "at" && !Number.isFinite(schedule.at_ms)) {
      setOperationError("Choose a valid run time.");
      return;
    }
    void runCronAction(
      async () => {
        await addCronJob({ name: trimmedName, message: trimmedMessage, schedule });
        setName("");
        setMessage("");
      },
      "Cron job added",
    );
  };

  return (
    <div className="ops-panel">
      <div className="ops-section">
        <div className="ops-section-header">
          <span>Heartbeat</span>
          <button type="button" onClick={refreshCron}>Refresh</button>
        </div>
        {heartbeat.loading && <div className="workspace-small-empty">Loading…</div>}
        {heartbeat.error && <div className="workspace-small-empty workspace-small-empty--error">Error: {heartbeat.error}</div>}
        {heartbeat.data && (
          <>
            <div className="ops-stats">
              <span>{heartbeat.data.enabled ? "enabled" : "disabled"}</span>
              <span>{heartbeat.data.running ? "running" : "stopped"}</span>
              <span>{heartbeat.data.interval_seconds}s</span>
            </div>
            <div className="ops-row">
              <label className="ops-check">
                <input
                  type="checkbox"
                  checked={heartbeat.data.enabled}
                  onChange={(e) => void runCronAction(
                    () => updateHeartbeat({ enabled: e.target.checked }),
                    e.target.checked ? "Heartbeat enabled" : "Heartbeat disabled",
                  )}
                />
                <span>Enabled</span>
              </label>
              <input
                className="ops-input ops-input--small"
                value={heartbeatInterval}
                onChange={(e) => setHeartbeatInterval(e.target.value)}
                inputMode="numeric"
                placeholder="seconds"
              />
              <button
                type="button"
                onClick={() => void runCronAction(
                  () => updateHeartbeat({
                    intervalSeconds: Math.max(
                      1,
                      Number(heartbeatInterval) || heartbeat.data?.interval_seconds || 1,
                    ),
                  }),
                  "Heartbeat interval updated",
                )}
              >
                Save
              </button>
            </div>
          </>
        )}
      </div>

      {operationError && <div className="list-operation-error">{operationError}</div>}
      {operationMessage && <div className="ops-success">{operationMessage}</div>}

      <div className="ops-section">
        <div className="ops-section-heading">New Cron Job</div>
        <input
          className="ops-input"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Name"
        />
        <textarea
          className="ops-textarea"
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          placeholder="Message for the agent..."
          rows={3}
        />
        <div className="ops-row">
          <select
            className="ops-input"
            value={scheduleKind}
            onChange={(e) => setScheduleKind(e.target.value as "every" | "at")}
          >
            <option value="every">Every</option>
            <option value="at">At</option>
          </select>
          {scheduleKind === "every" ? (
            <input
              className="ops-input"
              value={everyMinutes}
              onChange={(e) => setEveryMinutes(e.target.value)}
              inputMode="numeric"
              placeholder="minutes"
            />
          ) : (
            <input
              className="ops-input"
              type="datetime-local"
              value={atLocal}
              onChange={(e) => setAtLocal(e.target.value)}
            />
          )}
        </div>
        <div className="ops-actions">
          <button
            type="button"
            disabled={!name.trim() || !message.trim()}
            onClick={submitCronJob}
          >
            Add job
          </button>
        </div>
      </div>

      <div className="ops-section">
        <div className="ops-section-heading">Jobs</div>
        {cron.loading && <div className="workspace-small-empty">Loading…</div>}
        {cron.error && <div className="workspace-small-empty workspace-small-empty--error">Error: {cron.error}</div>}
        {cron.data && cron.data.jobs.length === 0 && <div className="workspace-small-empty">No cron jobs</div>}
        {cron.data && cron.data.jobs.length > 0 && (
          <ul className="ops-list">
            {cron.data.jobs.map((job: CronJob) => (
              <li key={job.id} className="ops-list-item ops-list-item--stacked">
                <span className="ops-list-title">{job.name}</span>
                <span className="ops-list-body">{job.payload.message}</span>
                <span className="ops-list-meta">
                  {job.enabled ? "on" : "off"} · {formatCronSchedule(job)} · next {formatTimestamp(job.state.next_run_at_ms)}
                </span>
                {job.state.last_error && (
                  <span className="ops-list-error">{job.state.last_error}</span>
                )}
                <div className="ops-actions">
                  <button
                    type="button"
                    onClick={() => void runCronAction(
                      () => updateCronJob(job.id, !job.enabled),
                      job.enabled ? "Cron job disabled" : "Cron job enabled",
                    )}
                  >
                    {job.enabled ? "Disable" : "Enable"}
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      if (!window.confirm(`Delete cron job "${job.name}"?`)) return;
                      void runCronAction(
                        () => deleteCronJob(job.id),
                        "Cron job deleted",
                      );
                    }}
                  >
                    Delete
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function memoryResultTitle(result: MemorySearchResult): string {
  return result.text
    ?? result.better_action
    ?? result.trigger
    ?? result.excerpt
    ?? result.date
    ?? result.id
    ?? "memory result";
}

function formatCronSchedule(job: CronJob): string {
  if (job.schedule.kind === "every" && job.schedule.every_ms) {
    return `every ${Math.round(job.schedule.every_ms / 60000)}m`;
  }
  if (job.schedule.kind === "at" && job.schedule.at_ms) {
    return `at ${formatTimestamp(job.schedule.at_ms)}`;
  }
  return job.schedule.kind;
}

function formatTimestamp(value?: number | null): string {
  if (!value) return "none";
  return new Date(value).toLocaleString();
}
