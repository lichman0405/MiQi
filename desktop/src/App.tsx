import { useState, useEffect, useCallback, useReducer, useRef } from "react";
import { Rail } from "./components/Rail";
import { ListPanel } from "./components/ListPanel";
import { ChatSurface } from "./components/ChatSurface";
import { Inspector } from "./components/Inspector";
import { SettingsView } from "./components/SettingsView";
import { ConnectionStatus } from "./components/ConnectionStatus";
import { initTransport, onStatusChange, type TransportStatus } from "./lib/ipc";
import {
  chatReducer,
  INITIAL_CHAT_STATE,
  subscribeRuntimeEvents,
  createSession,
  loadSession as loadSessionRpc,
  type ChatState,
  type RuntimeEvent,
} from "./lib/chat-state";
import {
  canSwitchSession,
  shouldApplySessionLoadResponse,
  shouldClearChatAfterSessionDelete,
} from "./lib/session-ux";
import "./App.css";

export type RailTab = "chats" | "files" | "tools" | "memory" | "cron" | "settings";

export function App() {
  const [activeTab, setActiveTab] = useState<RailTab>("chats");
  const [theme, setTheme] = useState<"light" | "dark">("light");
  const [transportStatus, setTransportStatus] = useState<TransportStatus>("connecting");
  const [selectedWorkspaceFile, setSelectedWorkspaceFile] = useState<string | null>(null);
  const [workspaceRefreshKey, setWorkspaceRefreshKey] = useState(0);

  // Chat state (per active session)
  const [chatState, dispatch] = useReducer(chatReducer, INITIAL_CHAT_STATE);

  // Session refresh key — bump to trigger ListPanel re-fetch
  const [sessionRefreshKey, setSessionRefreshKey] = useState(0);
  const sessionLoadRequestRef = useRef(0);
  const sessionActionsDisabled = !canSwitchSession(chatState.executionStatus);

  // Initialize IPC transport on mount
  useEffect(() => {
    let mounted = true;

    async function init() {
      const status = await initTransport();
      if (mounted) setTransportStatus(status);
    }

    init();

    const unsub = onStatusChange((status) => {
      if (mounted) setTransportStatus(status);
    });

    return () => {
      mounted = false;
      unsub();
    };
  }, []);

  // Subscribe to runtime events
  useEffect(() => {
    const unsub = subscribeRuntimeEvents((event: RuntimeEvent) => {
      dispatch({ type: "EVENT", event });

      // Refresh session list on session-changing events
      if (event.type === "RunCompleted" || event.type === "RunCancelled") {
        setSessionRefreshKey((k) => k + 1);
      }
    });
    return unsub;
  }, []);

  const toggleTheme = useCallback(() => {
    setTheme((t) => (t === "light" ? "dark" : "light"));
  }, []);

  // Session interaction handlers
  const handleNewChat = useCallback(async () => {
    if (!canSwitchSession(chatState.executionStatus)) {
      dispatch({ type: "SET_ERROR", message: "Cannot switch sessions while a run is active." });
      return;
    }
    const key = `desktop:${Date.now()}`;
    const requestId = sessionLoadRequestRef.current + 1;
    sessionLoadRequestRef.current = requestId;
    try {
      const result = await createSession(key, "New chat");
      if (!shouldApplySessionLoadResponse(requestId, sessionLoadRequestRef.current, key, result.key)) {
        return;
      }
      dispatch({ type: "RESET", sessionKey: result.key, sessionTitle: result.title });
      setSessionRefreshKey((k) => k + 1);
    } catch {
      if (requestId !== sessionLoadRequestRef.current) return;
      dispatch({ type: "RESET", sessionKey: key, sessionTitle: "New chat" });
    }
  }, [chatState.executionStatus]);

  const handleSelectSession = useCallback(async (sessionKey: string, title?: string) => {
    if (chatState.sessionKey === sessionKey) return;
    if (!canSwitchSession(chatState.executionStatus)) {
      dispatch({ type: "SET_ERROR", message: "Cannot switch sessions while a run is active." });
      return;
    }
    const requestId = sessionLoadRequestRef.current + 1;
    sessionLoadRequestRef.current = requestId;
    try {
      const result = await loadSessionRpc(sessionKey);
      if (!shouldApplySessionLoadResponse(requestId, sessionLoadRequestRef.current, sessionKey, result.key)) {
        return;
      }
      const messages: ChatState["messages"] = (result.messages || []).map((msg, idx) => ({
        id: `hist-${idx}`,
        role: (msg.role === "user" || msg.role === "assistant" ? msg.role : "assistant") as "user" | "assistant",
        content: typeof msg.content === "string" ? msg.content : "",
        status: "complete" as const,
        toolCalls: [],
      }));
      dispatch({
        type: "LOAD_MESSAGES",
        sessionKey,
        messages,
        sessionTitle: result.title,
      });
    } catch {
      if (requestId !== sessionLoadRequestRef.current) return;
      dispatch({ type: "RESET", sessionKey, sessionTitle: title ?? sessionKey });
    }
  }, [chatState.sessionKey, chatState.executionStatus]);

  const handleSessionRenamed = useCallback((sessionKey: string, title: string) => {
    dispatch({ type: "SET_SESSION_TITLE", sessionKey, title });
    setSessionRefreshKey((k) => k + 1);
  }, []);

  const handleSessionRemoved = useCallback((sessionKey: string) => {
    sessionLoadRequestRef.current += 1;
    setSessionRefreshKey((k) => k + 1);
    if (shouldClearChatAfterSessionDelete(chatState.sessionKey, sessionKey)) {
      dispatch({ type: "CLEAR_SESSION" });
    }
  }, [chatState.sessionKey]);

  return (
    <div data-theme={theme} className="app-layout">
      <Rail activeTab={activeTab} onTabChange={setActiveTab} onToggleTheme={toggleTheme} theme={theme} />
      {activeTab === "settings" ? (
        <SettingsView />
      ) : (
        <>
          <ListPanel
            activeTab={activeTab}
            sessionRefreshKey={sessionRefreshKey}
            activeSessionKey={chatState.sessionKey}
            sessionActionsDisabled={sessionActionsDisabled}
            onNewChat={handleNewChat}
            onSelectSession={handleSelectSession}
            onSessionRenamed={handleSessionRenamed}
            onSessionRemoved={handleSessionRemoved}
            activeWorkspaceFile={selectedWorkspaceFile}
            onSelectWorkspaceFile={setSelectedWorkspaceFile}
            onWorkspaceChanged={() => setWorkspaceRefreshKey((k) => k + 1)}
          />
          <ChatSurface chatState={chatState} dispatch={dispatch} />
          <Inspector
            selectedWorkspaceFile={selectedWorkspaceFile}
            workspaceRefreshKey={workspaceRefreshKey}
          />
        </>
      )}
      <ConnectionStatus status={transportStatus} />
    </div>
  );
}
