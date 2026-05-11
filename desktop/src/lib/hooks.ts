/**
 * React hooks for IPC data fetching.
 * Each hook makes a real RPC call and falls back to empty data on error.
 */

import { useState, useEffect, useCallback, useRef } from "react";
import { request, subscribe, type JsonRpcEvent } from "./ipc";
import type {
  CronListResult,
  HeartbeatStatusResult,
  McpStatusResult,
  MemoryStatusResult,
  SnapshotListResponse,
  LessonListResponse,
} from "./ops-state";
import {
  previewWorkspaceFile,
  readContextBootstrap,
  shouldApplyWorkspacePreviewResponse,
  type ContextBootstrapListResult,
  type ContextBootstrapReadResult,
  type ContextSkillsListResult,
  type ContextStatusResult,
  type WorkspaceFileListResult,
  type WorkspaceIndexResult,
  type WorkspacePreviewResult,
  type WorkspaceStatusResult,
} from "./workspace-state";

// ── Generic async fetch hook ─────────────────────────────────────────────

interface FetchState<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
}

function useIpcFetch<T>(method: string, params?: Record<string, unknown>): FetchState<T> & { refresh: () => void } {
  const [state, setState] = useState<FetchState<T>>({
    data: null,
    loading: true,
    error: null,
  });
  const [refreshKey, setRefreshKey] = useState(0);
  const paramsKey = JSON.stringify(params ?? {});

  const refresh = useCallback(() => setRefreshKey((k) => k + 1), []);

  useEffect(() => {
    let cancelled = false;

    setState({ data: null, loading: true, error: null });

    request<T>(method, params)
      .then((data) => {
        if (!cancelled) setState({ data, loading: false, error: null });
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          const message = err instanceof Error ? err.message : String(err);
          setState({ data: null, loading: false, error: message });
        }
      });

    return () => { cancelled = true; };
  }, [method, paramsKey, refreshKey]);

  return { ...state, refresh };
}

// ── Event-driven refresh hook ────────────────────────────────────────────

function useIpcRefreshOnEvent(refresh: () => void, eventType: string): void {
  useEffect(() => {
    const unsub = subscribe((event: JsonRpcEvent) => {
      // Runtime and state-change events are JSON-RPC method-style notifications.
      if (event.method === eventType) {
        refresh();
      }
      // Legacy runtime_event envelope is still tolerated for older mock/dev builds.
      if (event.method === "runtime_event" && (event.params as Record<string, unknown>)?.type === eventType) {
        refresh();
      }
    });
    return unsub;
  }, [refresh, eventType]);
}

// ── Specific data hooks ──────────────────────────────────────────────────

export interface AppStatus {
  status: string;
  model: string;
  workspace: string;
  agent_name: string;
}

export function useAppStatus() {
  return useIpcFetch<AppStatus>("app.status");
}

export interface SessionInfo {
  key: string;
  title: string;
  preview?: string;
  source?: string;
  updated_at?: string;
  message_count: number;
  archived?: boolean;
}

export interface SessionListResult {
  sessions: SessionInfo[];
  count: number;
}

export function useSessionList() {
  const fetch = useIpcFetch<SessionListResult>("session.list", { include_archived: false });
  useIpcRefreshOnEvent(fetch.refresh, "SessionChanged");
  return fetch;
}

export interface ToolInfo {
  name: string;
  description?: string;
}

export interface ToolListResult {
  tools: ToolInfo[];
  count: number;
}

export function useToolList() {
  return useIpcFetch<ToolListResult>("tool.list");
}

export function useMcpStatus() {
  return useIpcFetch<McpStatusResult>("mcp.status");
}

export function useMemoryStatus() {
  const fetch = useIpcFetch<MemoryStatusResult>("memory.status");
  useIpcRefreshOnEvent(fetch.refresh, "MemoryChanged");
  return fetch;
}

export function useMemorySnapshot(limit = 50) {
  const fetch = useIpcFetch<SnapshotListResponse>("memory.listSnapshot", { limit });
  useIpcRefreshOnEvent(fetch.refresh, "MemoryChanged");
  return fetch;
}

export function useMemoryLessons(includeDisabled = true, limit = 50) {
  const fetch = useIpcFetch<LessonListResponse>("memory.listLessons", {
    include_disabled: includeDisabled,
    limit,
  });
  useIpcRefreshOnEvent(fetch.refresh, "MemoryChanged");
  return fetch;
}

export function useCronList() {
  const fetch = useIpcFetch<CronListResult>("cron.list", { include_disabled: true });
  useIpcRefreshOnEvent(fetch.refresh, "CronJobChanged");
  return fetch;
}

export function useHeartbeatStatus() {
  return useIpcFetch<HeartbeatStatusResult>("heartbeat.status");
}

export function useWorkspaceStatus() {
  const fetch = useIpcFetch<WorkspaceStatusResult>("workspace.status");
  useIpcRefreshOnEvent(fetch.refresh, "WorkspaceIndexChanged");
  return fetch;
}

export function useWorkspaceIndex() {
  const fetch = useIpcFetch<WorkspaceIndexResult>("workspace.index", { depth: 6 });
  useIpcRefreshOnEvent(fetch.refresh, "WorkspaceIndexChanged");
  return fetch;
}

export function useWorkspacePinned() {
  return useIpcFetch<WorkspaceFileListResult>("workspace.listPinned");
}

export function useWorkspaceRecent(limit = 20) {
  return useIpcFetch<WorkspaceFileListResult>("workspace.listRecent", { limit });
}

export function useWorkspacePreview(path: string | null) {
  const [state, setState] = useState<FetchState<WorkspacePreviewResult>>({
    data: null,
    loading: false,
    error: null,
  });
  const [refreshKey, setRefreshKey] = useState(0);
  const requestSeq = useRef(0);
  const refresh = useCallback(() => setRefreshKey((k) => k + 1), []);

  useEffect(() => {
    if (!path) {
      requestSeq.current += 1;
      setState({ data: null, loading: false, error: null });
      return;
    }

    const requestId = requestSeq.current + 1;
    requestSeq.current = requestId;
    setState({ data: null, loading: true, error: null });

    previewWorkspaceFile(path)
      .then((data) => {
        if (!shouldApplyWorkspacePreviewResponse(
          requestId,
          requestSeq.current,
          path,
          path,
          data.path,
        )) {
          return;
        }
        setState({ data, loading: false, error: null });
      })
      .catch((err: unknown) => {
        if (requestId !== requestSeq.current) return;
        const message = err instanceof Error ? err.message : String(err);
        setState({ data: null, loading: false, error: message });
      });
  }, [path, refreshKey]);

  return { ...state, refresh };
}

export function useContextStatus() {
  return useIpcFetch<ContextStatusResult>("context.status");
}

export function useContextBootstrap() {
  return useIpcFetch<ContextBootstrapListResult>("context.listBootstrap");
}

export function useContextBootstrapPreview(name: string | null) {
  const [state, setState] = useState<FetchState<ContextBootstrapReadResult>>({
    data: null,
    loading: false,
    error: null,
  });
  const [refreshKey, setRefreshKey] = useState(0);
  const refresh = useCallback(() => setRefreshKey((k) => k + 1), []);

  useEffect(() => {
    if (!name) {
      setState({ data: null, loading: false, error: null });
      return;
    }

    let cancelled = false;
    setState({ data: null, loading: true, error: null });
    readContextBootstrap(name)
      .then((data) => {
        if (!cancelled) setState({ data, loading: false, error: null });
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          const message = err instanceof Error ? err.message : String(err);
          setState({ data: null, loading: false, error: message });
        }
      });

    return () => { cancelled = true; };
  }, [name, refreshKey]);

  return { ...state, refresh };
}

export function useContextSkills() {
  return useIpcFetch<ContextSkillsListResult>("context.listSkills");
}

// ── Config ─────────────────────────────────────────────────────────────────

export interface ConfigData {
  agents?: {
    defaults?: {
      name?: string;
      model?: string;
      workspace?: string;
      maxTokens?: number;
      max_tokens?: number;
      temperature?: number;
    };
  };
  providers?: Record<string, {
    apiKey?: string;
    api_key?: string;
    apiBase?: string | null;
    api_base?: string;
    extraHeaders?: Record<string, string>;
    extra_headers?: Record<string, string>;
  }>;
  tools?: {
    restrictToWorkspace?: boolean;
    restrict_to_workspace?: boolean;
  };
  heartbeat?: {
    enabled?: boolean;
    intervalSeconds?: number;
    interval_seconds?: number;
  };
  [key: string]: unknown;
}

export function useConfigRead() {
  return useIpcFetch<ConfigData>("config.read");
}
