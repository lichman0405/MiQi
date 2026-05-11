import { request } from "./ipc";

export interface MemoryStatusResult {
  ltm_items: number;
  snapshot_exists: boolean;
  lessons_count: number;
  self_improvement_enabled: boolean;
  short_term_sessions: number;
  pending_sessions: number;
  dirty_updates: number;
}

export interface MemorySearchResult {
  source: "snapshot" | "lesson" | "daily_note" | string;
  id?: string;
  text?: string;
  trigger?: string;
  better_action?: string;
  excerpt?: string;
  date?: string;
  hits?: number;
  confidence?: number;
  enabled?: boolean;
  updated_at?: string;
}

export interface MemorySearchResponse {
  results: MemorySearchResult[];
  count: number;
  query: string;
}

export interface SnapshotItem {
  id: string;
  text: string;
  session_key?: string;
  source?: string;
  hits?: number;
  created_at?: string;
  updated_at?: string;
}

export interface SnapshotListResponse {
  items: SnapshotItem[];
  count: number;
}

export interface LessonItem {
  id: string;
  trigger: string;
  bad_action?: string;
  better_action: string;
  confidence?: number;
  enabled?: boolean;
  scope?: string;
  source?: string;
  session_key?: string;
  created_at?: string;
  updated_at?: string;
}

export interface LessonListResponse {
  lessons: LessonItem[];
  count: number;
}

export interface MemoryActionResult {
  action: string;
  [key: string]: unknown;
}

export interface McpServerStatus {
  configured: boolean;
  connected: boolean;
}

export interface McpStatusResult {
  connected: boolean;
  connecting: boolean;
  servers: Record<string, McpServerStatus>;
  retry_after: number;
}

export interface CronJob {
  id: string;
  name: string;
  enabled: boolean;
  schedule: {
    kind: "at" | "every" | "cron" | string;
    at_ms?: number | null;
    every_ms?: number | null;
    expr?: string | null;
    tz?: string | null;
  };
  payload: {
    kind: string;
    message: string;
    deliver?: boolean;
    channel?: string | null;
    to?: string | null;
  };
  state: {
    next_run_at_ms?: number | null;
    last_run_at_ms?: number | null;
    last_status?: string | null;
    last_error?: string | null;
  };
  created_at_ms?: number;
}

export interface CronListResult {
  jobs: CronJob[];
  count: number;
}

export interface HeartbeatStatusResult {
  enabled: boolean;
  interval_seconds: number;
  running: boolean;
}

export async function searchMemory(query: string, limit = 20): Promise<MemorySearchResponse> {
  return request<MemorySearchResponse>("memory.search", { query, limit });
}

export async function getMemoryStatus(): Promise<MemoryStatusResult> {
  return request<MemoryStatusResult>("memory.status");
}

export async function updateMemory(params: {
  text: string;
  action?: "remember" | "append_today" | "learn_lesson";
  betterAction?: string;
  badAction?: string;
}): Promise<MemoryActionResult> {
  return request<MemoryActionResult>("memory.update", {
    text: params.text,
    ...(params.action ? { action: params.action } : {}),
    ...(params.betterAction ? { better_action: params.betterAction } : {}),
    ...(params.badAction ? { bad_action: params.badAction } : {}),
  });
}

export async function rememberMemory(text: string): Promise<MemoryActionResult> {
  return request<MemoryActionResult>("memory.remember", { text });
}

export async function appendTodayMemory(content: string): Promise<MemoryActionResult> {
  return request<MemoryActionResult>("memory.appendToday", { content });
}

export async function learnMemoryLesson(params: {
  trigger: string;
  betterAction: string;
  badAction?: string;
}): Promise<MemoryActionResult> {
  return request<MemoryActionResult>("memory.learnLesson", {
    trigger: params.trigger,
    better_action: params.betterAction,
    ...(params.badAction ? { bad_action: params.badAction } : {}),
  });
}

export async function listMemorySnapshot(limit = 50): Promise<SnapshotListResponse> {
  return request<SnapshotListResponse>("memory.listSnapshot", { limit });
}

export async function listMemoryLessons(
  includeDisabled = true,
  limit = 50,
): Promise<LessonListResponse> {
  return request<LessonListResponse>("memory.listLessons", {
    include_disabled: includeDisabled,
    limit,
  });
}

export async function deleteMemorySnapshotItem(itemId: string): Promise<MemoryActionResult> {
  return request<MemoryActionResult>("memory.deleteSnapshotItem", { item_id: itemId });
}

export async function deleteMemoryLesson(lessonId: string): Promise<MemoryActionResult> {
  return request<MemoryActionResult>("memory.deleteLesson", { lesson_id: lessonId });
}

export async function setMemoryLessonEnabled(
  lessonId: string,
  enabled: boolean,
): Promise<MemoryActionResult> {
  return request<MemoryActionResult>("memory.setLessonEnabled", {
    lesson_id: lessonId,
    enabled,
  });
}

export async function listCronJobs(includeDisabled = true): Promise<CronListResult> {
  return request<CronListResult>("cron.list", { include_disabled: includeDisabled });
}

export async function addCronJob(params: {
  name: string;
  message: string;
  schedule: { kind: "every"; every_ms: number } | { kind: "at"; at_ms: number };
}): Promise<{ success: boolean; job_id: string }> {
  return request<{ success: boolean; job_id: string }>("cron.add", {
    name: params.name,
    message: params.message,
    schedule: params.schedule,
  });
}

export async function updateCronJob(
  jobId: string,
  enabled: boolean,
): Promise<{ success: boolean; job_id: string }> {
  return request<{ success: boolean; job_id: string }>("cron.update", {
    job_id: jobId,
    enabled,
  });
}

export async function deleteCronJob(jobId: string): Promise<{ success: boolean; job_id: string }> {
  return request<{ success: boolean; job_id: string }>("cron.delete", { job_id: jobId });
}

export async function updateHeartbeat(params: {
  enabled?: boolean;
  intervalSeconds?: number;
}): Promise<{ success: boolean }> {
  return request<{ success: boolean }>("heartbeat.update", {
    ...(params.enabled !== undefined ? { enabled: params.enabled } : {}),
    ...(params.intervalSeconds !== undefined ? { interval_seconds: params.intervalSeconds } : {}),
  });
}

export async function getHeartbeatStatus(): Promise<HeartbeatStatusResult> {
  return request<HeartbeatStatusResult>("heartbeat.status");
}

export async function getMcpStatus(): Promise<McpStatusResult> {
  return request<McpStatusResult>("mcp.status");
}
