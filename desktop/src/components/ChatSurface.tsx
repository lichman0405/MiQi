import { useState, useEffect, useRef, useCallback } from "react";
import {
  sendChatMessage,
  cancelExecution,
  regenerateChat,
  approveAction,
  denyAction,
  type ChatState,
  type ToolCallInfo,
  type ApprovalInfo,
} from "../lib/chat-state";
import "./ChatSurface.css";

interface ChatSurfaceProps {
  chatState: ChatState;
  dispatch: React.Dispatch<Parameters<typeof import("../lib/chat-state").chatReducer>[1]>;
}

export function ChatSurface({ chatState, dispatch }: ChatSurfaceProps) {
  const [input, setInput] = useState("");
  const transcriptRef = useRef<HTMLDivElement>(null);
  const isStarting = chatState.executionStatus === "starting";
  const isRunning = chatState.executionStatus === "running" || chatState.executionStatus === "cancelling";
  const isBusy = isStarting || isRunning;

  // Auto-scroll on new messages/content
  const contentKey = chatState.messages.map((m) => `${m.id}:${m.content.length}:${m.status}`).join("|");
  useEffect(() => {
    if (transcriptRef.current) {
      transcriptRef.current.scrollTop = transcriptRef.current.scrollHeight;
    }
  }, [contentKey]);

  const handleSubmit = useCallback(async (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = input.trim();
    if (!trimmed || !chatState.sessionKey || isBusy) return;

    setInput("");
    dispatch({ type: "ADD_USER_MESSAGE", id: `user-${Date.now()}`, content: trimmed });

    try {
      await sendChatMessage(trimmed, chatState.sessionKey);
    } catch (err) {
      dispatch({ type: "SET_ERROR", message: err instanceof Error ? err.message : String(err) });
    }
  }, [input, chatState.sessionKey, isBusy, dispatch]);

  const handleCancel = useCallback(async () => {
    if (!chatState.executionId) return;
    try {
      await cancelExecution(chatState.executionId);
    } catch {
      // Error event will arrive via event stream
    }
  }, [chatState.executionId]);

  const handleRegenerate = useCallback(async () => {
    if (!chatState.sessionKey || isBusy) return;
    try {
      dispatch({ type: "START_EXECUTION" });
      await regenerateChat(chatState.sessionKey);
    } catch (err) {
      dispatch({ type: "SET_ERROR", message: err instanceof Error ? err.message : String(err) });
    }
  }, [chatState.sessionKey, isBusy, dispatch]);

  return (
    <main className="chat-surface">
      <div className="chat-transcript" ref={transcriptRef}>
        {chatState.messages.length === 0 && (
          <div className="chat-empty">
            <p>Start a conversation or select a session.</p>
          </div>
        )}
        {chatState.messages.map((msg) => (
          <div key={msg.id} className={`chat-message chat-message--${msg.role}`}>
            <div className="chat-message-avatar">
              {msg.role === "user" ? "U" : "M"}
            </div>
            <div className="chat-message-body">
              {msg.toolCalls.map((tc) => (
                <ToolCard
                  key={tc.id}
                  tool={tc}
                  approval={chatState.approvals.find(
                    (a) => a.toolCallId === tc.id || (a.toolName === tc.name && !a.toolCallId)
                  )}
                  onApprove={(choice) => handleApprove(chatState.approvals, tc, choice)}
                  onDeny={() => handleDeny(chatState.approvals, tc)}
                />
              ))}
              {msg.content && (
                <div className="chat-message-content">
                  {msg.content}
                  {msg.status === "streaming" && <span className="chat-cursor">▍</span>}
                </div>
              )}
              {msg.status === "cancelled" && msg.role === "assistant" && (
                <div className="chat-message-cancelled">Cancelled</div>
              )}
            </div>
          </div>
        ))}
      </div>

      {chatState.error && (
        <div className="chat-error-bar">
          <span className="chat-error-text">{chatState.error}</span>
          <button className="chat-error-dismiss" onClick={() => dispatch({ type: "CLEAR_ERROR" })}>Dismiss</button>
        </div>
      )}

      <div className="chat-header-bar">
        {chatState.sessionTitle && (
          <span className="chat-session-title">{chatState.sessionTitle}</span>
        )}
        {isBusy && (
          <span className="chat-run-indicator">{isStarting ? "Starting..." : "Running..."}</span>
        )}
      </div>

      <div className="chat-composer-area">
        <form className="chat-composer" onSubmit={handleSubmit}>
          <textarea
            className="chat-composer-input"
            placeholder={chatState.sessionKey ? "Send a message..." : "Start a chat first..."}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            rows={1}
            disabled={!chatState.sessionKey || isBusy}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                (e.target as HTMLTextAreaElement).form?.requestSubmit();
              }
            }}
          />
          <div className="chat-composer-actions">
            {isRunning && chatState.executionId ? (
              <button type="button" className="composer-btn" title="Cancel" aria-label="Cancel" onClick={handleCancel}>⏹</button>
            ) : (
              <button type="button" className="composer-btn" title="Regenerate" aria-label="Regenerate" onClick={handleRegenerate} disabled={!chatState.sessionKey || chatState.messages.length === 0 || isBusy}>🔄</button>
            )}
            <button type="submit" className="composer-btn composer-btn--send" title="Send" aria-label="Send" disabled={!input.trim() || !chatState.sessionKey || isBusy}>↑</button>
          </div>
        </form>
      </div>
    </main>
  );
}

function handleApprove(approvals: ApprovalInfo[], tc: ToolCallInfo, choice: "once" | "session" | "always") {
  const approval = approvals.find((a) => (a.toolCallId === tc.id || (a.toolName === tc.name && !a.toolCallId)) && !a.resolved);
  if (!approval) return;
  approveAction(approval.approvalId, choice).catch(() => {});
}

function handleDeny(approvals: ApprovalInfo[], tc: ToolCallInfo) {
  const approval = approvals.find((a) => (a.toolCallId === tc.id || (a.toolName === tc.name && !a.toolCallId)) && !a.resolved);
  if (!approval) return;
  denyAction(approval.approvalId).catch(() => {});
}

function ToolCard({
  tool,
  approval,
  onApprove,
  onDeny,
}: {
  tool: ToolCallInfo;
  approval?: ApprovalInfo;
  onApprove: (choice: "once" | "session" | "always") => void;
  onDeny: () => void;
}) {
  return (
    <div className={`tool-card tool-card--${tool.status}`}>
      <div className="tool-card-header">
        <span className="tool-card-name">🔧 {tool.name}</span>
        <span className={`tool-card-status tool-card-status--${tool.status}`}>
          {statusLabel(tool.status)}
        </span>
        {tool.elapsedSeconds != null && tool.status === "running" && (
          <span className="tool-card-duration">{tool.elapsedSeconds.toFixed(1)}s</span>
        )}
        {tool.status === "complete" && (
          <span className="tool-card-duration">done</span>
        )}
      </div>
      {tool.status === "running" && (
        <div className="tool-card-progress">
          <div className="tool-card-progress-bar" />
        </div>
      )}
      {tool.progressMessage && tool.status === "running" && (
        <div className="tool-card-progress-msg">{tool.progressMessage}</div>
      )}
      {tool.resultPreview && (tool.status === "complete" || tool.status === "error") && (
        <div className={`tool-card-result ${tool.is_error ? "tool-card-result--error" : ""}`}>
          {tool.resultPreview}
        </div>
      )}
      {tool.status === "pending_approval" && approval && !approval.resolved && (
        <div className="tool-card-approval">
          <span className="approval-label">Dangerous command — requires approval</span>
          {approval.commandPreview && (
            <code className="approval-preview">{approval.commandPreview}</code>
          )}
          {approval.patternDescription && (
            <span className="approval-description">{approval.patternDescription}</span>
          )}
          <div className="approval-actions">
            <button className="approval-btn approval-btn--deny" onClick={() => onDeny()}>Deny</button>
            <button className="approval-btn approval-btn--once" onClick={() => onApprove("once")}>Approve once</button>
            <button className="approval-btn approval-btn--session" onClick={() => onApprove("session")}>This session</button>
            <button className="approval-btn approval-btn--always" onClick={() => onApprove("always")}>Always</button>
          </div>
        </div>
      )}
      {tool.status === "pending_approval" && approval?.resolved && (
        <div className={`tool-card-resolved ${approval.decision === "deny" ? "tool-card-resolved--denied" : ""}`}>
          {approval.decision === "deny" ? "Denied" : `Approved (${approval.decision})`}
        </div>
      )}
    </div>
  );
}

function statusLabel(status: ToolCallInfo["status"]): string {
  const labels: Record<ToolCallInfo["status"], string> = {
    running: "Running",
    complete: "Done",
    error: "Error",
    pending_approval: "Approval",
  };
  return labels[status];
}
