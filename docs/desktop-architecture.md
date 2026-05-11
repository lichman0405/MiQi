# Desktop Architecture

MiQi Desktop should add a third product entry point beside the existing CLI and gateway. The desktop app must reuse the Python runtime rather than reimplement agent behavior in the frontend.

!!! note "Implementation status"
    This document specifies the target architecture for the desktop work. It intentionally separates runtime foundations from UI implementation so the feature can be built in safe phases.

## Target Topology

```
Desktop Window (Tauri + React)
        |
        | stdio NDJSON JSON-RPC
        v
miqi desktop-backend --stdio
        |
        v
Application Services
        |
        +-- AgentService / ExecutionManager
        +-- SessionService
        +-- WorkspaceService
        +-- ContextService
        +-- MemoryService
        +-- ToolApprovalService
        +-- Tool/MCP/Cron/Heartbeat services
        |
        v
Existing MiQi Runtime
        |
        +-- AgentLoop
        +-- LLM providers
        +-- ToolRegistry
        +-- MemoryStore
        +-- SessionManager / SessionDB
        +-- CronService / HeartbeatService
```

## Entry Points

MiQi should support three independent entry points:

| Entry point | User interface | Runtime path |
|---|---|---|
| CLI | `miqi agent` | direct `AgentLoop.process_direct` or message bus |
| Gateway | `miqi gateway` | channel adapters + `MessageBus` + `AgentLoop` |
| Desktop | `miqi desktop-backend --stdio` sidecar | JSON-RPC + application services + `AgentLoop` |

The desktop entry point should not replace or weaken the CLI/gateway paths.

## Runtime Factory

The current CLI and gateway commands manually assemble the provider, bus, cron service, session manager, and agent loop. Desktop work should first extract a shared runtime factory so all entry points construct compatible runtime objects.

The factory should handle:

- config loading;
- provider construction;
- fallback and routing hooks where enabled;
- `MessageBus` construction;
- `CronService` construction;
- `SessionManager` or SQLite session backend selection;
- `AgentLoop` construction;
- MCP server configuration;
- graceful shutdown.

The factory should be small and boring. It should not become a service locator that hides ownership.

## Project Workspace vs Data Root

The current `agents.defaults.workspace` acts as both an agent workspace and runtime data root. Desktop should introduce a clearer distinction:

| Concept | Purpose | Example |
|---|---|---|
| Project root | Files the user wants the agent to inspect or modify | `C:\Users\me\code\project` |
| MiQi data root | MiQi memory, sessions, cron, skills, logs, runtime data | `~/.miqi/workspace` |

Backward compatibility rule: if no separate project root exists, keep using `agents.defaults.workspace` as before.

The file tools and shell working directory should default to the project root. Memory, sessions, cron jobs, bootstrap files, custom skills, and logs should live under the data root.

## IPC Protocol

Default transport: newline-delimited JSON-RPC over stdio.

Reasons:

- no default network listener;
- simple Tauri sidecar lifecycle;
- easy integration tests;
- portable across Windows, macOS, and Linux;
- clear separation between UI process and Python runtime.

Optional WebSocket transport may exist for development, but it must not be required for normal desktop operation.

### Sidecar Transport (implemented)

The frontend `SidecarTransport` (in `desktop/src/lib/ipc.ts`) launches the Python sidecar via Tauri's `@tauri-apps/plugin-shell` and communicates over newline-delimited JSON-RPC:

1. On app startup, `initTransport()` attempts to `Command.sidecar("binaries/miqi-desktop-backend", ["--stdio"])`.
2. The `Command` object's `stdout` event emitter receives arbitrary chunks (not necessarily complete lines). Each chunk is appended to an internal buffer.
3. The buffer is drained on every chunk: lines are split on `\n`, supporting one-chunk-many-lines, half-line-across-chunks, and empty lines.
4. Each complete line (after trimming) is parsed as JSON. Malformed lines are silently ignored.
5. Parsed messages with `id` + (`result` or `error`) are dispatched as responses.
6. Parsed messages with `method` (no `id`) are dispatched as server-initiated events.
7. RPC requests are written to the child's stdin via `child.write()`.
8. If the sidecar exits or errors, all pending requests are rejected with a transport error and the transport sets status to `disconnected`, then auto-reconnects after 3 seconds.
9. When the transport is replaced via `setTransport()`, all pending requests from the previous transport are rejected to avoid permanent loading states.
10. If Tauri APIs are unavailable (plain Vite dev server), `initTransport()` falls back to `MockTransport`.

The transport never exposes stderr content in the UI — backend logs may contain config values or paths that should not appear in frontend diagnostics.

The Tauri sidecar is declared as `bundle.externalBin =
["binaries/miqi-desktop-backend"]` and allowed in
`src-tauri/capabilities/default.json` with `shell:allow-spawn`,
`shell:allow-stdin-write`, and `shell:allow-kill`. During development,
`npm run sidecar:dev` builds the small launcher source under
`src-tauri/sidecars/miqi-desktop-backend/` and copies a platform-suffixed
binary into `src-tauri/binaries/`. That generated binary is ignored by git. The
launcher keeps the product transport as stdio: it runs the repo Python
environment with `miqi desktop-backend --stdio` and inherits stdin/stdout from
Tauri.

Runtime events use method-style JSON-RPC notifications. The event type is the
notification `method`; the `params` object contains event fields and does not
wrap them in `params.type`:

```json
{"jsonrpc":"2.0","method":"MessageDelta","params":{"execution_id":"exec-1","delta":"hello"}}
```

The frontend may tolerate the older `method: "runtime_event"` envelope while
parsing, but the sidecar transport and `MockTransport` should emit method-style
events so mock mode cannot mask sidecar contract bugs.

### Frontend Data Hooks

React components fetch data through typed hooks in `desktop/src/lib/hooks.ts` rather than importing mock arrays. Each hook calls `request(method, params)` and returns `{ data, loading, error, refresh }`. Hooks that depend on runtime events (e.g. `useSessionList` refreshes on `SessionChanged`) subscribe to the event stream automatically.

### Chat State Management

The chat UI is driven by a centralized state reducer in `desktop/src/lib/chat-state.ts`. The reducer processes:

- **User actions**: `ADD_USER_MESSAGE`, `RESET` (new session), `LOAD_MESSAGES` (session switch)
- **Runtime events**: `RunStarted`, `MessageDelta`, `MessageFinal`, `ToolCallStarted`, `ToolProgress`, `ToolResult`, `ApprovalRequested`, `ApprovalResolved`, `RunCompleted`, `RunCancelled`, `Error`
- **Approval decisions**: `APPROVAL_RESOLVED`
- **Error handling**: `SET_ERROR`, `CLEAR_ERROR`

The `App` component holds the chat state via `useReducer` and subscribes to runtime events via `subscribeRuntimeEvents()`. The `ChatSurface` component receives the state and dispatch as props.

Session switching is handled by `handleSelectSession` in `App.tsx`, which calls `session.load` to fetch messages and dispatches `LOAD_MESSAGES` to replace the chat state atomically. The `LOAD_MESSAGES` reducer action resets execution/approval/error state and sets the new session key, title, and messages. A monotonic request id guards rapid session switching so late `session.load` responses cannot replace the currently selected session. Switching is blocked while an execution is starting, running, or cancelling so runtime events stay attached to the active chat state.

Approval cards in tool calls are wired to `chat.approve` (with choice: `once`, `session`, `always`) and `chat.deny`. The backend responds with `ApprovalResolved` events that update the approval and tool call status in the reducer. Sensitive command text is never shown verbatim — only the backend-provided `command_preview` (redacted) and `pattern_description` are displayed.

Workspace and context inspection are wired through typed helpers in `desktop/src/lib/workspace-state.ts` and hooks in `desktop/src/lib/hooks.ts`. The Files tab opens a new current root through `workspace.open`, renders only backend-provided paths from `workspace.index`, loads previews with `workspace.preview`, and pins/unpins files through `workspace.pinFile` / `workspace.unpinFile`. Opening a workspace clears the selected file and refreshes workspace status, index, pinned, recent, and Inspector file/context panels. Preview requests use a monotonic request id guard so late responses cannot replace the latest selected file. The Inspector Context tab reads `context.status`, `context.listBootstrap`, `context.readBootstrap`, and `context.listSkills`; the Inspector Files tab reads pinned/recent lists and a short preview for the current selected file.

The Inspector Activity tab is a local event stream over `subscribeRuntimeEvents()`. It displays timestamped summaries for `RunStarted`, `RunCompleted`, `RunCancelled`, `ToolCallStarted`, `ToolProgress`, `ToolResult`, `ApprovalRequested`, `ApprovalResolved`, and `Error` notifications. It is intentionally read-only and does not create a second execution state machine.

Memory, Cron, Heartbeat, and MCP operations are wired through typed helpers in `desktop/src/lib/ops-state.ts` and hooks in `desktop/src/lib/hooks.ts`. The Memory tab calls live memory RPCs for search, update/remember, daily notes, lessons, snapshot listing, toggles, and deletes; successful mutations emit `MemoryChanged` so panels can refresh. The Cron tab calls `cron.list`, `cron.add`, `cron.update`, and `cron.delete` for minimal `every_ms` / `at_ms` job management; successful add/update/delete operations emit `CronJobChanged`. The Cron tab also embeds `heartbeat.status` / `heartbeat.update` controls. MCP remains read-only because the backend exposes only `mcp.status`; the Inspector Tools panel renders that status and provides manual Refresh for `mcp.status` and `tool.list`.

### Message Types

The protocol should define:

- request;
- response;
- error response;
- event notification.

Every long-running call should return a stable execution id and emit progress events. Errors should include machine-readable `code`, human-readable `message`, and optional redacted details.

### Required RPC Groups

| Group | Methods |
|---|---|
| App | `app.status` |
| Config | `config.read`, `config.write`, `config.testProvider` |
| Workspace | `workspace.list`, `workspace.open`, `workspace.index`, `workspace.preview`, `workspace.pinFile`, `workspace.unpinFile`, `workspace.listPinned`, `workspace.listRecent` |
| Session | `session.list`, `session.create`, `session.rename`, `session.delete`, `session.search`, `session.load` |
| Chat | `chat.send`, `chat.cancel`, `chat.regenerate` |
| Tools | `tool.list` |
| MCP | `mcp.status` |
| Context | `context.status`, `context.listBootstrap`, `context.readBootstrap`, `context.listSkills` |
| Memory | `memory.status`, `memory.search`, `memory.update`, `memory.remember`, `memory.appendToday`, `memory.learnLesson`, `memory.listSnapshot`, `memory.listLessons`, `memory.deleteSnapshotItem`, `memory.deleteLesson`, `memory.setLessonEnabled` |
| Cron | `cron.list`, `cron.add`, `cron.update`, `cron.delete` |
| Heartbeat | `heartbeat.status`, `heartbeat.update` |

`config.write` accepts nested partial config updates matching the Pydantic
schema. It accepts snake_case field names and camelCase aliases inside that
nested shape, but it does not accept dot-path keys. For example,
`tools.restrict_to_workspace` is sent as:

```json
{"updates":{"tools":{"restrict_to_workspace":true}}}
```

## Structured Events

The existing `on_progress` hook is string-oriented. Desktop needs structured events while the CLI can keep rendering strings.

Target events:

| Event | Purpose |
|---|---|
| `RunStarted` | A user request began execution |
| `RunCompleted` | Execution finished successfully |
| `RunCancelled` | Execution was cancelled |
| `QueueUpdated` | Active/pending task state changed |
| `MessageDelta` | Streaming assistant content or reasoning-safe text delta |
| `MessageFinal` | Final assistant message |
| `ToolCallStarted` | Tool execution began |
| `ToolProgress` | Tool progress, including MCP heartbeat updates |
| `ToolResult` | Tool finished with result or error |
| `ApprovalRequested` | UI must approve or deny a sensitive action |
| `ApprovalResolved` | Approval request was answered |
| `SessionChanged` | Session metadata or messages changed |
| `MemoryChanged` | Memory snapshot/lessons changed |
| `WorkspaceIndexChanged` | File index changed |
| `McpStatusChanged` | Reserved for a future stable MCP connection-state source; current UI uses `mcp.status` manual refresh |
| `CronJobChanged` | Cron job changed |
| `Error` | Recoverable or fatal backend error |

## Agent Service

`AgentService` should wrap single-turn and multi-turn desktop runs. It should not duplicate the agent loop.

Responsibilities:

- create execution ids;
- submit user messages;
- connect execution to session ids;
- forward structured events;
- support cancellation;
- handle regenerate/retry;
- preserve session writes and memory updates;
- translate legacy string progress into structured events when needed.

## Execution Manager

`ExecutionManager` should track active and recent executions:

- `queued`;
- `running`;
- `waiting_for_approval`;
- `cancelling`;
- `cancelled`;
- `completed`;
- `failed`.

Cancellation should be checked:

- before LLM calls;
- after LLM calls;
- before tool execution;
- during cancellable tools;
- before saving final state.

Shell subprocesses should be terminated gracefully, then killed if needed, with stdout/stderr pipes drained safely.

## Approval Service

The desktop app needs explicit approval events for dangerous commands and other sensitive tools.

The existing `miqi/agent/command_approval.py` should remain the source of dangerous command detection. Desktop work should add a UI-agnostic `ToolApprovalService` that can produce and resolve `ApprovalRequested` events.

Approval decisions:

- deny;
- approve once;
- approve for session;
- approve always.

Permanent approval must be persisted through config and saved with safe file permissions. Denied commands must not execute and must be recorded as denied tool results.

## Streaming Providers

The provider interface should add optional streaming capability without forcing every provider to implement it at once.

Recommended shape:

- define a response chunk model in `miqi/providers/base.py`;
- add `supports_streaming` or an optional `chat_stream` method;
- implement OpenAI-compatible streaming first;
- implement Anthropic streaming second;
- keep blocking fallback for all providers.

The agent loop should still be able to handle tool calls and final content for blocking providers.

## Session Storage

The JSONL `SessionManager` is readable and stable. It should remain compatible with existing sessions.

Desktop search and filtering should use the existing SQLite + FTS5 backend when practical. Desktop work should add a service layer that can:

- list sessions with source, title, preview, and last active time;
- search message text;
- load full conversations;
- migrate or lazily import JSONL sessions;
- rename, pin, archive, delete, and export sessions.

The UI should distinguish session source values such as `desktop`, `cli`, `feishu`, `cron`, `heartbeat`, and `system`.

## Workspace Service

The workspace service should index the project root and expose a safe file model to the UI.

Responsibilities:

- list configured workspaces;
- open/switch workspace;
- build file tree;
- read text previews;
- track recent agent-touched files;
- pin files/directories into context;
- apply ignore rules;
- enforce workspace restrictions.

The service should never encourage the frontend to bypass `restrictToWorkspace` or direct filesystem tool safety checks.

## Context Service

The context service should inspect and explain the context assembled for a run:

- bootstrap files;
- active skills;
- memory hits;
- session history window;
- pinned files;
- attached media;
- context budget;
- compression state.

It may expose context management actions, but the canonical prompt assembly remains in `ContextBuilder`.

## Desktop Shell Responsibilities

Tauri/Rust should stay thin:

- start/stop Python sidecar;
- relay stdio messages;
- manage window lifecycle;
- manage tray and global shortcut;
- show native notifications;
- open native file dialogs;
- expose safe OS integrations.

Business logic belongs in the Python backend or React state layer, not in Rust.

## Security Requirements

- No default network listener for desktop IPC.
- Redact secrets in logs and error details.
- Make MCP stdio command/env configuration visible and understandable.
- Require approval for dangerous commands.
- Respect project root and `restrictToWorkspace`.
- Keep CLI and gateway security behavior intact.

## Testing Strategy

Backend tests:

- runtime factory construction;
- JSON-RPC schema validation;
- IPC request/response/error behavior;
- event ordering;
- fake-provider chat execution;
- cancellation;
- approval decisions;
- session migration/search;
- workspace path restrictions.

Frontend tests:

- RPC client state transitions;
- session list and active chat state;
- streaming message rendering;
- tool cards;
- approval cards;
- settings validation;
- empty/loading/error states.

Manual packaging checks should begin with Windows, then expand to macOS and Linux.
