/**
 * Chat state management for the desktop UI.
 *
 * Centralises the active session's messages, tool calls, approvals,
 * and execution state. Driven by RPC calls and structured runtime events.
 */

import { request, subscribe, type JsonRpcEvent } from "./ipc";

// ── Structured event types (aligned with miqi/events/models.py) ────────────

export interface RunStartedEvent {
  type: "RunStarted";
  execution_id: string;
  session_key: string;
  channel?: string;
  preview?: string;
}

export interface RunCompletedEvent {
  type: "RunCompleted";
  execution_id: string;
  session_key: string;
  response_preview?: string;
}

export interface RunCancelledEvent {
  type: "RunCancelled";
  execution_id: string;
  session_key: string;
  reason?: string;
}

export interface MessageDeltaEvent {
  type: "MessageDelta";
  execution_id: string;
  delta: string;
}

export interface MessageFinalEvent {
  type: "MessageFinal";
  execution_id: string;
  content: string;
}

export interface ToolCallStartedEvent {
  type: "ToolCallStarted";
  execution_id: string;
  tool_name: string;
  tool_call_id?: string;
}

export interface ToolProgressEvent {
  type: "ToolProgress";
  execution_id: string;
  tool_name: string;
  tool_call_id?: string;
  elapsed_seconds?: number;
  message?: string;
}

export interface ToolResultEvent {
  type: "ToolResult";
  execution_id: string;
  tool_name: string;
  tool_call_id?: string;
  preview?: string;
  is_error?: boolean;
}

export interface ApprovalRequestedEvent {
  type: "ApprovalRequested";
  approval_id: string;
  execution_id: string;
  tool_name: string;
  tool_call_id?: string;
  pattern_description?: string;
  command_preview?: string;
}

export interface ApprovalResolvedEvent {
  type: "ApprovalResolved";
  approval_id: string;
  execution_id: string;
  tool_call_id?: string;
  decision: "once" | "session" | "always" | "deny";
}

export interface ErrorEvent {
  type: "Error";
  execution_id?: string;
  message: string;
  source?: string;
}

export type RuntimeEvent =
  | RunStartedEvent
  | RunCompletedEvent
  | RunCancelledEvent
  | MessageDeltaEvent
  | MessageFinalEvent
  | ToolCallStartedEvent
  | ToolProgressEvent
  | ToolResultEvent
  | ApprovalRequestedEvent
  | ApprovalResolvedEvent
  | ErrorEvent;

const RUNTIME_EVENT_TYPES = new Set<string>([
  "RunStarted",
  "RunCompleted",
  "RunCancelled",
  "MessageDelta",
  "MessageFinal",
  "ToolCallStarted",
  "ToolProgress",
  "ToolResult",
  "ApprovalRequested",
  "ApprovalResolved",
  "Error",
]);

function isRuntimeEventType(value: unknown): value is RuntimeEvent["type"] {
  return typeof value === "string" && RUNTIME_EVENT_TYPES.has(value);
}

// ── UI-facing message/tool/approval models ─────────────────────────────────

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  status: "streaming" | "complete" | "cancelled" | "error";
  toolCalls: ToolCallInfo[];
}

export interface ToolCallInfo {
  id: string;
  name: string;
  status: "running" | "complete" | "error" | "pending_approval";
  elapsedSeconds?: number;
  progressMessage?: string;
  resultPreview?: string;
  is_error?: boolean;
}

export interface ApprovalInfo {
  approvalId: string;
  executionId: string;
  toolName: string;
  toolCallId: string;
  patternDescription: string;
  commandPreview: string;
  resolved: boolean;
  decision?: "once" | "session" | "always" | "deny";
}

export type ExecutionStatus =
  | "idle"
  | "starting"
  | "running"
  | "cancelling"
  | "completed"
  | "cancelled"
  | "failed";

// ── Chat state ─────────────────────────────────────────────────────────────

export interface ChatState {
  sessionKey: string | null;
  sessionTitle: string;
  messages: ChatMessage[];
  approvals: ApprovalInfo[];
  executionId: string | null;
  executionStatus: ExecutionStatus;
  error: string | null;
}

export const INITIAL_CHAT_STATE: ChatState = {
  sessionKey: null,
  sessionTitle: "",
  messages: [],
  approvals: [],
  executionId: null,
  executionStatus: "idle",
  error: null,
};

// ── State reducer ──────────────────────────────────────────────────────────

type ChatAction =
  | { type: "RESET"; sessionKey: string; sessionTitle?: string }
  | { type: "CLEAR_SESSION" }
  | { type: "START_EXECUTION" }
  | { type: "ADD_USER_MESSAGE"; id: string; content: string }
  | { type: "EVENT"; event: RuntimeEvent }
  | { type: "SET_ERROR"; message: string }
  | { type: "CLEAR_ERROR" }
  | { type: "SET_SESSION_TITLE"; sessionKey: string; title: string }
  | { type: "APPROVAL_RESOLVED"; approvalId: string; decision: "once" | "session" | "always" | "deny" }
  | { type: "LOAD_MESSAGES"; sessionKey: string; messages: ChatMessage[]; sessionTitle: string };

export function chatReducer(state: ChatState, action: ChatAction): ChatState {
  switch (action.type) {
    case "CLEAR_SESSION":
      return INITIAL_CHAT_STATE;

    case "RESET":
      return {
        ...INITIAL_CHAT_STATE,
        sessionKey: action.sessionKey,
        sessionTitle: action.sessionTitle ?? "",
      };

    case "START_EXECUTION":
      return {
        ...state,
        executionId: null,
        executionStatus: "starting",
        error: null,
      };

    case "ADD_USER_MESSAGE":
      return {
        ...state,
        executionId: null,
        executionStatus: "starting",
        error: null,
        messages: [
          ...state.messages,
          {
            id: action.id,
            role: "user",
            content: action.content,
            status: "complete",
            toolCalls: [],
          },
        ],
      };

    case "LOAD_MESSAGES":
      return {
        ...INITIAL_CHAT_STATE,
        sessionKey: action.sessionKey,
        sessionTitle: action.sessionTitle,
        messages: action.messages,
      };

    case "SET_ERROR":
      return {
        ...state,
        error: action.message,
        executionId: state.executionStatus === "starting" ? null : state.executionId,
        executionStatus: state.executionStatus === "starting" ? "failed" : state.executionStatus,
      };

    case "CLEAR_ERROR":
      return { ...state, error: null };

    case "SET_SESSION_TITLE":
      if (state.sessionKey !== action.sessionKey) return state;
      return { ...state, sessionTitle: action.title };

    case "APPROVAL_RESOLVED": {
      return {
        ...state,
        approvals: state.approvals.map((a) =>
          a.approvalId === action.approvalId
            ? { ...a, resolved: true, decision: action.decision }
            : a
        ),
        messages: state.messages.map((msg) => ({
          ...msg,
          toolCalls: msg.toolCalls.map((tc) =>
            tc.id === state.approvals.find((a) => a.approvalId === action.approvalId)?.toolCallId
              ? {
                  ...tc,
                  status: action.decision === "deny" ? "error" as const : "running" as const,
                }
              : tc
          ),
        })),
      };
    }

    case "EVENT":
      return applyEvent(state, action.event);
  }
}

function applyEvent(state: ChatState, event: RuntimeEvent): ChatState {
  switch (event.type) {
    case "RunStarted":
      if (!matchesActiveSession(state, event.session_key)) return state;
      return {
        ...state,
        executionId: event.execution_id,
        executionStatus: "running",
        messages: [
          ...state.messages,
          {
            id: `asst-${event.execution_id}`,
            role: "assistant",
            content: "",
            status: "streaming",
            toolCalls: [],
          },
        ],
      };

    case "MessageDelta": {
      if (state.executionId !== event.execution_id) return state;
      return {
        ...state,
        messages: state.messages.map((msg) =>
          msg.id === `asst-${event.execution_id}` && msg.status === "streaming"
            ? { ...msg, content: msg.content + event.delta }
            : msg
        ),
      };
    }

    case "MessageFinal": {
      if (state.executionId !== event.execution_id) return state;
      return {
        ...state,
        messages: state.messages.map((msg) =>
          msg.id === `asst-${event.execution_id}`
            ? { ...msg, content: event.content, status: "complete" }
            : msg
        ),
      };
    }

    case "ToolCallStarted": {
      if (state.executionId !== event.execution_id) return state;
      const tcId = event.tool_call_id || event.tool_name;
      return {
        ...state,
        messages: state.messages.map((msg) =>
          msg.id === `asst-${event.execution_id}`
            ? {
                ...msg,
                toolCalls: [
                  ...msg.toolCalls,
                  {
                    id: tcId,
                    name: event.tool_name,
                    status: "running",
                  },
                ],
              }
            : msg
        ),
      };
    }

    case "ToolProgress": {
      if (state.executionId !== event.execution_id) return state;
      const tcId = event.tool_call_id || event.tool_name;
      return {
        ...state,
        messages: state.messages.map((msg) =>
          msg.id === `asst-${event.execution_id}`
            ? {
                ...msg,
                toolCalls: msg.toolCalls.map((tc) =>
                  tc.id === tcId
                    ? {
                        ...tc,
                        elapsedSeconds: event.elapsed_seconds,
                        progressMessage: event.message,
                      }
                    : tc
                ),
              }
            : msg
        ),
      };
    }

    case "ToolResult": {
      if (state.executionId !== event.execution_id) return state;
      const tcId = event.tool_call_id || event.tool_name;
      return {
        ...state,
        messages: state.messages.map((msg) =>
          msg.id === `asst-${event.execution_id}`
            ? {
                ...msg,
                toolCalls: msg.toolCalls.map((tc) =>
                  tc.id === tcId
                    ? {
                        ...tc,
                        status: event.is_error ? "error" : "complete",
                        resultPreview: event.preview,
                        is_error: event.is_error,
                      }
                    : tc
                ),
              }
            : msg
        ),
      };
    }

    case "ApprovalRequested": {
      if (state.executionId !== event.execution_id) return state;
      return {
        ...state,
        approvals: [
          ...state.approvals.filter((a) => !a.resolved),
          {
            approvalId: event.approval_id,
            executionId: event.execution_id,
            toolName: event.tool_name,
            toolCallId: event.tool_call_id || "",
            patternDescription: event.pattern_description ?? "",
            commandPreview: event.command_preview ?? "",
            resolved: false,
          },
        ],
        messages: state.messages.map((msg) => {
          if (msg.id !== `asst-${event.execution_id}`) return msg;
          const tcId = event.tool_call_id || event.tool_name;
          return {
            ...msg,
            toolCalls: msg.toolCalls.map((tc) =>
              tc.id === tcId ? { ...tc, status: "pending_approval" as const } : tc
            ),
          };
        }),
      };
    }

    case "ApprovalResolved": {
      if (state.executionId !== event.execution_id) return state;
      const approval = state.approvals.find((a) => a.approvalId === event.approval_id);
      if (!approval) return state;
      const tcId = approval.toolCallId || approval.toolName;
      const newStatus = event.decision === "deny" ? "error" as const : "running" as const;
      return {
        ...state,
        approvals: state.approvals.map((a) =>
          a.approvalId === event.approval_id
            ? { ...a, resolved: true, decision: event.decision }
            : a
        ),
        messages: state.messages.map((msg) => ({
          ...msg,
          toolCalls: msg.toolCalls.map((tc) =>
            tc.id === tcId ? { ...tc, status: newStatus } : tc
          ),
        })),
      };
    }

    case "RunCompleted":
      if (!matchesActiveSession(state, event.session_key)) return state;
      if (state.executionId !== event.execution_id) return state;
      return {
        ...state,
        executionId: null,
        executionStatus: "completed",
      };

    case "RunCancelled":
      if (!matchesActiveSession(state, event.session_key)) return state;
      if (state.executionId !== event.execution_id) return state;
      return {
        ...state,
        executionId: null,
        executionStatus: "cancelled",
        messages: state.messages.map((msg) =>
          msg.id === `asst-${event.execution_id}` && msg.status === "streaming"
            ? { ...msg, status: "cancelled" }
            : msg
        ),
      };

    case "Error":
      if (event.execution_id && state.executionId !== event.execution_id) return state;
      return {
        ...state,
        error: event.message,
        executionStatus: state.executionId === event.execution_id ? "failed" : state.executionStatus,
      };

    default:
      return state;
  }
}

function matchesActiveSession(state: ChatState, eventSessionKey?: string): boolean {
  return !state.sessionKey || eventSessionKey === state.sessionKey;
}

// ── RPC action helpers ─────────────────────────────────────────────────────

export async function sendChatMessage(
  message: string,
  sessionKey: string,
): Promise<{ execution_id: string }> {
  return request<{ execution_id: string }>("chat.send", {
    message,
    session_key: sessionKey,
  });
}

export async function cancelExecution(executionId: string): Promise<{ success: boolean }> {
  return request<{ success: boolean }>("chat.cancel", { execution_id: executionId });
}

export async function regenerateChat(sessionKey: string): Promise<{ execution_id: string }> {
  return request<{ execution_id: string }>("chat.regenerate", { session_key: sessionKey });
}

export async function approveAction(
  approvalId: string,
  choice: "once" | "session" | "always",
): Promise<{ success: boolean; decision: string }> {
  return request<{ success: boolean; decision: string }>("chat.approve", {
    approval_id: approvalId,
    choice,
  });
}

export async function denyAction(
  approvalId: string,
): Promise<{ success: boolean; decision: string }> {
  return request<{ success: boolean; decision: string }>("chat.deny", {
    approval_id: approvalId,
  });
}

export async function createSession(
  key: string,
  title?: string,
): Promise<{ key: string; title: string }> {
  return request<{ key: string; title: string }>("session.create", { key, title });
}

export interface SessionInfoResult {
  key: string;
  title: string;
  preview?: string;
  source?: string;
  updated_at?: string;
  message_count: number;
  archived?: boolean;
}

export interface SessionListRpcResult {
  sessions: SessionInfoResult[];
  count: number;
  query?: string;
}

export async function listSessions(
  includeArchived = false,
): Promise<SessionListRpcResult> {
  return request<SessionListRpcResult>("session.list", {
    include_archived: includeArchived,
  });
}

export async function searchSessions(
  query: string,
  includeArchived = false,
): Promise<SessionListRpcResult> {
  return request<SessionListRpcResult>("session.search", {
    query,
    include_archived: includeArchived,
  });
}

export async function renameSession(
  key: string,
  title: string,
): Promise<{ key: string; title: string }> {
  return request<{ key: string; title: string }>("session.rename", { key, title });
}

export async function archiveSession(
  key: string,
): Promise<{ key: string; archived: boolean }> {
  return request<{ key: string; archived: boolean }>("session.archive", { key });
}

export async function unarchiveSession(
  key: string,
): Promise<{ key: string; archived: boolean }> {
  return request<{ key: string; archived: boolean }>("session.unarchive", { key });
}

export async function deleteSession(
  key: string,
): Promise<{ key: string; deleted: boolean }> {
  return request<{ key: string; deleted: boolean }>("session.delete", { key });
}

export async function loadSession(sessionKey: string): Promise<{
  key: string;
  title: string;
  source: string;
  updated_at: string;
  message_count: number;
  messages: Array<{
    role: string;
    content?: string;
    tool_calls?: unknown;
    tool_call_id?: string;
    name?: string;
    timestamp?: string;
  }>;
}> {
  return request("session.load", { key: sessionKey });
}

// ── Event subscription helper ──────────────────────────────────────────────

/**
 * Parse a JSON-RPC notification into a chat RuntimeEvent.
 *
 * The Python sidecar emits method-style notifications:
 *   { "jsonrpc": "2.0", "method": "RunStarted", "params": { ... } }
 *
 * The legacy runtime_event envelope is still accepted for compatibility:
 *   { "jsonrpc": "2.0", "method": "runtime_event", "params": { "type": "RunStarted", ... } }
 */
export function runtimeEventFromJsonRpcEvent(raw: JsonRpcEvent): RuntimeEvent | null {
  const params = raw.params as Record<string, unknown> | undefined;

  if (raw.method === "runtime_event") {
    const eventType = params?.type;
    if (!isRuntimeEventType(eventType)) return null;
    return { ...params, type: eventType } as unknown as RuntimeEvent;
  }

  if (!isRuntimeEventType(raw.method)) return null;
  return { ...(params ?? {}), type: raw.method } as unknown as RuntimeEvent;
}

/**
 * Subscribe to runtime events, parse them into typed RuntimeEvent objects,
 * and forward them to the given handler.
 */
export function subscribeRuntimeEvents(
  handler: (event: RuntimeEvent) => void,
): () => void {
  return subscribe((raw) => {
    const event = runtimeEventFromJsonRpcEvent(raw);
    if (event) handler(event);
  });
}
