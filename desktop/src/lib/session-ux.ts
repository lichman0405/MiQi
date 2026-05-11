import type { ExecutionStatus } from "./chat-state";

export function canSwitchSession(executionStatus: ExecutionStatus): boolean {
  return executionStatus !== "starting"
    && executionStatus !== "running"
    && executionStatus !== "cancelling";
}

export function shouldApplySessionLoadResponse(
  requestId: number,
  latestRequestId: number,
  requestedSessionKey: string,
  loadedSessionKey: string,
): boolean {
  return requestId === latestRequestId && requestedSessionKey === loadedSessionKey;
}

export function shouldClearChatAfterSessionDelete(
  activeSessionKey: string | null,
  deletedSessionKey: string,
): boolean {
  return activeSessionKey === deletedSessionKey;
}
