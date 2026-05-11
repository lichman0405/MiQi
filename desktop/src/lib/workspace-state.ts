import { request } from "./ipc";

export interface WorkspaceEntry {
  name: string;
  path: string;
  is_dir: boolean;
  is_symlink: boolean;
  size: number;
  modified: string;
}

export interface WorkspaceIndexResult {
  root: string;
  subdir: string;
  entries: WorkspaceEntry[];
  count: number;
  truncated: boolean;
}

export interface WorkspaceFileInfo {
  path: string;
  exists: boolean;
  is_dir: boolean;
  is_symlink: boolean;
  is_binary: boolean;
  size: number;
  modified: string;
}

export interface WorkspaceFileListResult {
  files: WorkspaceFileInfo[];
  count: number;
}

export interface WorkspacePreviewResult {
  path: string;
  exists: boolean;
  is_dir?: boolean;
  is_binary?: boolean;
  size?: number;
  truncated?: boolean;
  content?: string | null;
}

export interface WorkspaceStatusResult {
  project_root: string;
  exists: boolean;
  restrict_to_workspace: boolean;
  pinned_count: number;
  recent_count: number;
}

export interface ContextBootstrapFile {
  name: string;
  exists: boolean;
  source: string;
  has_workspace_override: boolean;
  size: number;
}

export interface ContextSkill {
  name?: string;
  id?: string;
  path?: string;
  description?: string;
  available?: boolean;
  reason?: string;
  [key: string]: unknown;
}

export interface ContextStatusResult {
  workspace: string;
  bootstrap_files: ContextBootstrapFile[];
  skills: ContextSkill[];
  memory: {
    ltm_items: number;
    lessons_count: number;
    self_improvement_enabled: boolean;
    snapshot_exists: boolean;
  };
  pinned_files: {
    count: number;
    files: string[];
  };
  budget: {
    context_limit_chars: number;
    estimated_usage: number;
  };
}

export interface ContextBootstrapListResult {
  files: ContextBootstrapFile[];
  count: number;
}

export interface ContextBootstrapReadResult extends ContextBootstrapFile {
  content: string | null;
  truncated: boolean;
}

export interface ContextSkillsListResult {
  skills: ContextSkill[];
  count: number;
}

export async function openWorkspace(path: string): Promise<WorkspaceStatusResult> {
  return request<WorkspaceStatusResult>("workspace.open", { path });
}

export async function indexWorkspace(options: {
  subdir?: string;
  depth?: number;
} = {}): Promise<WorkspaceIndexResult> {
  const params: Record<string, unknown> = { depth: options.depth ?? 6 };
  if (options.subdir) params.subdir = options.subdir;
  return request<WorkspaceIndexResult>("workspace.index", params);
}

export async function previewWorkspaceFile(path: string): Promise<WorkspacePreviewResult> {
  return request<WorkspacePreviewResult>("workspace.preview", { path });
}

export async function pinWorkspaceFile(path: string): Promise<{ path: string; pinned: boolean }> {
  return request<{ path: string; pinned: boolean }>("workspace.pinFile", { path });
}

export async function unpinWorkspaceFile(path: string): Promise<{ path: string; pinned: boolean }> {
  return request<{ path: string; pinned: boolean }>("workspace.unpinFile", { path });
}

export async function listPinnedWorkspaceFiles(): Promise<WorkspaceFileListResult> {
  return request<WorkspaceFileListResult>("workspace.listPinned");
}

export async function listRecentWorkspaceFiles(limit = 20): Promise<WorkspaceFileListResult> {
  return request<WorkspaceFileListResult>("workspace.listRecent", { limit });
}

export async function contextStatus(): Promise<ContextStatusResult> {
  return request<ContextStatusResult>("context.status");
}

export async function listContextBootstrap(): Promise<ContextBootstrapListResult> {
  return request<ContextBootstrapListResult>("context.listBootstrap");
}

export async function readContextBootstrap(name: string): Promise<ContextBootstrapReadResult> {
  return request<ContextBootstrapReadResult>("context.readBootstrap", { name });
}

export async function listContextSkills(): Promise<ContextSkillsListResult> {
  return request<ContextSkillsListResult>("context.listSkills");
}

export function shouldApplyWorkspacePreviewResponse(
  requestId: number,
  latestRequestId: number,
  requestedPath: string,
  selectedPath: string | null,
  previewPath: string,
): boolean {
  return requestId === latestRequestId
    && selectedPath === requestedPath
    && previewPath === requestedPath;
}
