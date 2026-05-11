# Desktop

MiQi Desktop is a native desktop entry point for the MiQi runtime. It makes the existing agent, memory, skills, tools, MCP servers, cron jobs, and chat sessions usable from a local desktop application while preserving the current CLI and gateway workflows.

This page describes the intended user-facing behavior. See [Desktop Architecture](desktop-architecture.md) for the runtime design and [Desktop Design](desktop-design.md) for the UI system.

!!! note "Implementation status"
    The desktop MVP is scoped to the local Tauri shell, Python sidecar, and stdio JSON-RPC transport. The frontend has both a **SidecarTransport** (connected to the Python sidecar via Tauri's shell plugin) and a **MockTransport** (for non-Tauri development), but MVP validation uses the real sidecar path. Chat/session, Files workspace inspection, Inspector Context, Activity, tools, memory, cron, heartbeat, and settings surfaces use live JSON-RPC methods where listed below.

## MVP Scope

The current MVP includes local chat/session management, streaming runtime
events, approval cards, workspace tree/preview/pin/recent/open, context summary
and bootstrap file preview, tools/MCP read-only status, memory operations,
cron job basics, heartbeat controls, and provider/workspace settings.

Post-MVP deferred items are tray integration, global shortcuts, native
notifications, full MCP server management UI, installer packaging, and richer
workspace/file operations. These are not required for the MVP gate.

## Development Setup

### Prerequisites

- Node.js >= 20 and npm >= 10
- Rust toolchain (rustc + cargo >= 1.77)
- On Windows: WebView2 is required (pre-installed on Windows 11; install separately on Windows 10)
- MiQi Python environment set up via `uv sync --extra dev`

### Running in Development

```bash
cd desktop
npm install
npm run dev          # Vite dev server only (no Tauri window)
npm run typecheck   # TypeScript check
npm run build       # Vite production build
```

The Vite dev server runs the frontend with **MockTransport** because Tauri APIs are not available outside the native shell. The connection status indicator in the bottom-right corner will show "Mock mode".

To launch the full Tauri desktop window (which connects to the real Python sidecar):

```powershell
cd desktop
npm run sidecar:dev  # Builds the local Tauri sidecar launcher into src-tauri/binaries/
npm run tauri dev    # Compiles Rust + opens desktop window
```

This starts the Vite dev server and the Tauri shell, which renders the React UI in a system WebView. The Python sidecar (`miqi desktop-backend --stdio`) is launched automatically via Tauri's shell plugin, and the frontend communicates with it over newline-delimited JSON-RPC through stdio.

`npm run sidecar:dev` compiles a tiny local launcher from `src-tauri/sidecars/miqi-desktop-backend/` and copies the generated platform-suffixed executable to `src-tauri/binaries/miqi-desktop-backend-<target-triple>.exe` on Windows. The generated executable is ignored by git; the committed source is the launcher code and prepare script. The launcher prefers `MIQI_DESKTOP_PYTHON`, the currently activated virtual environment or conda environment, then the repo `.venv`, and finally falls back to `python` so local desktop dev does not depend on a single environment layout.

!!! warning "Windows WDAC limitation"
    On Windows systems with Application Control Policy (WDAC/SiPolicy), `npm run tauri dev` may fail because the Rust build script for `vswhom-sys` (a transitive Tauri dependency) triggers os error 4551. This is an environment-level restriction, not a code issue. The frontend builds and runs correctly under Vite. Full Tauri native builds require either a WDAC policy update or a non-WDAC development environment.

### Real Sidecar Smoke Test

Use the smoke probe before manual UI checks to verify the Python backend speaks
newline-delimited JSON-RPC over stdio without touching the user's real config:

```powershell
Set-Location 'C:\Users\lishi\code\MiQi'
.venv\Scripts\python.exe scripts\desktop_stdio_smoke.py
```

The probe creates temporary `MIQI_CONFIG_PATH` and `MIQI_DATA_DIR` values with a
local-provider config, starts `miqi desktop-backend --stdio`, and checks:

- malformed JSON returns JSON-RPC parse error and the process stays alive;
- `app.status`, `session.list`, `tool.list`, and `memory.status` return success;
- an unknown method returns JSON-RPC method-not-found;
- closing stdin shuts the sidecar down cleanly.

To verify the Tauri dev sidecar launcher path specifically:

```powershell
Set-Location 'C:\Users\lishi\code\MiQi\desktop'
npm run sidecar:dev
Set-Location 'C:\Users\lishi\code\MiQi'
.venv\Scripts\python.exe scripts\desktop_stdio_smoke.py --dev-sidecar
```

For a native window smoke test:

```powershell
Set-Location 'C:\Users\lishi\code\MiQi\desktop'
npm run sidecar:dev
npm run tauri dev
```

The bottom-right connection badge should show **Connected**. If it shows
**Mock mode**, the app is running in plain Vite or the Tauri sidecar could not
be spawned. Expected initial RPC behavior is that status, sessions, tools, and
memory panels load from the real backend. This smoke path remains local desktop
IPC only; it does not require a hosted web app or a default HTTP/WebSocket
product transport.

If the badge turns red **Disconnected** instead of **Connected**, run the
`--dev-sidecar` smoke check first. If the smoke check passes, the Python
backend and launcher are healthy and the remaining issue is in the Tauri sidecar
spawn layer rather than the MiQi backend JSON-RPC service.

### Project Structure

```
desktop/
├── src-tauri/          # Tauri 2 Rust shell
│   ├── src/
│   │   ├── lib.rs      # Tauri app setup (sidecar relay goes here)
│   │   └── main.rs     # Entry point
│   ├── binaries/       # Generated Tauri sidecar binaries (ignored by git)
│   ├── sidecars/       # Source for local dev sidecar launchers
│   ├── Cargo.toml
│   ├── tauri.conf.json
│   ├── capabilities/
│   └── icons/          # MiQi-branded placeholder icons
├── src/                # React + TypeScript frontend
│   ├── components/     # UI components
│   ├── lib/
│   │   ├── ipc.ts      # JSON-RPC IPC client (SidecarTransport + MockTransport)
│   │   └── hooks.ts    # React hooks for IPC data fetching
│   ├── styles/
│   │   ├── theme.css   # Light/dark CSS tokens (Microscopic Era palette)
│   │   └── global.css
│   ├── App.tsx
│   └── main.tsx
├── package.json
├── tsconfig.json
├── vite.config.ts
└── index.html
```

### IPC Client

The frontend IPC client (`src/lib/ipc.ts`) provides:

- TypeScript types for JSON-RPC 2.0 request/response/event
- `request(method, params)` API that returns a typed Promise
- `subscribe(handler)` for server-initiated event streaming
- `onStatusChange(handler)` for transport connection status
- `initTransport()` — auto-detects Tauri and connects to the sidecar; falls back to MockTransport
- `SidecarTransport` — real transport over Tauri shell plugin (newline-delimited JSON-RPC over sidecar stdio)
- `MockTransport` — returns mock data for non-Tauri environments

### Transport Selection

On startup, the app calls `initTransport()` which:

1. Tries to spawn the Python sidecar via Tauri's `@tauri-apps/plugin-shell`
2. If the sidecar connects successfully → status = `connected`
3. If Tauri is not available (plain Vite dev) → status = `mock`
4. If the sidecar exits or errors → status = `disconnected`, auto-reconnect after 3s

The connection status indicator (bottom-right corner) shows the current transport state:
- **Connected** (green) — real sidecar
- **Mock mode** (gray) — mock data, no real backend
- **Disconnected** (red) — sidecar connection lost
- **Connecting...** / **Reconnecting...** (yellow pulse) — in progress

### React Hooks

The `src/lib/hooks.ts` module provides typed React hooks for IPC data:

- `useAppStatus()` — calls `app.status`
- `useSessionList()` — calls `session.list`, refreshes on `SessionChanged` events
- `useToolList()` — calls `tool.list`
- `useMcpStatus()` — calls `mcp.status`; the Inspector Tools panel exposes manual refresh
- `useMemoryStatus()` — calls `memory.status`, refreshes on `MemoryChanged` events
- `useMemorySnapshot()`, `useMemoryLessons()` — drive the Memory tab and Inspector Memory panel, refresh on `MemoryChanged` events
- `useCronList()` — calls `cron.list`, refreshes on `CronJobChanged` events
- `useHeartbeatStatus()` — calls `heartbeat.status`
- `useWorkspaceStatus()` — calls `workspace.status`, refreshes on `WorkspaceIndexChanged` events
- `useWorkspaceIndex()`, `useWorkspacePreview()`, `useWorkspacePinned()`, `useWorkspaceRecent()` — drive the Files tab and Inspector file summary
- `useContextStatus()`, `useContextBootstrap()`, `useContextBootstrapPreview()`, `useContextSkills()` — drive the Inspector Context panel

Each hook returns `{ data, loading, error, refresh }`. Components use these hooks instead of hardcoded mock arrays.

### RPC Methods Wired to UI

| Method | Component | Status |
|---|---|---|
| `app.status` | — | Available via hook |
| `session.list` | ListPanel (Chats tab) | Live RPC |
| `session.search` | ListPanel (search box) | Live RPC |
| `session.create` | ListPanel ("+" button) | Live RPC |
| `session.load` | ListPanel (click session item) | Live RPC |
| `session.rename` | ListPanel (session row action) | Live RPC |
| `session.archive` | ListPanel (session row action) | Live RPC |
| `session.unarchive` | ListPanel (session row action) | Live RPC |
| `session.delete` | ListPanel (session row action) | Live RPC |
| `chat.send` | ChatSurface (composer submit) | Live RPC + events |
| `chat.cancel` | ChatSurface (cancel button) | Live RPC |
| `chat.regenerate` | ChatSurface (regenerate button) | Live RPC |
| `chat.approve` | ChatSurface (approval card) | Live RPC |
| `chat.deny` | ChatSurface (approval card) | Live RPC |
| `config.read` | SettingsView (Provider, Workspace) | Live RPC |
| `config.write` | SettingsView (Save buttons) | Live RPC |
| `config.testProvider` | SettingsView (Test Connection) | Live RPC |
| `tool.list` | ListPanel (Tools tab), Inspector (Tools tab) | Live RPC |
| `mcp.status` | Inspector (Tools tab, Refresh button) | Read-only RPC |
| `memory.status` | ListPanel (Memory tab), Inspector (Memory tab) | Live RPC |
| `memory.search` | ListPanel (Memory tab search) | Live RPC |
| `memory.update` | ListPanel (Memory tab remember action) | Live RPC |
| `memory.remember` | ListPanel (Memory tab remember action) | Live RPC |
| `memory.appendToday` | ListPanel (Memory tab daily note action) | Live RPC |
| `memory.learnLesson` | ListPanel (Memory tab lesson form) | Live RPC |
| `memory.listSnapshot` | ListPanel + Inspector (Memory panels) | Live RPC |
| `memory.listLessons` | ListPanel + Inspector (Memory panels) | Live RPC |
| `memory.deleteSnapshotItem` | ListPanel (Memory tab snapshot action) | Live RPC |
| `memory.deleteLesson` | ListPanel (Memory tab lesson action) | Live RPC |
| `memory.setLessonEnabled` | ListPanel (Memory tab lesson action) | Live RPC |
| `cron.list` | ListPanel (Cron tab) | Live RPC |
| `cron.add` | ListPanel (Cron tab new job form) | Live RPC |
| `cron.update` | ListPanel (Cron tab enable/disable) | Live RPC |
| `cron.delete` | ListPanel (Cron tab delete action) | Live RPC |
| `heartbeat.status` | ListPanel (Cron tab heartbeat section) | Live RPC |
| `heartbeat.update` | ListPanel (Cron tab heartbeat section) | Live RPC |
| `workspace.status` | ListPanel (Files tab) | Live RPC |
| `workspace.open` | ListPanel (Files tab workspace path Open button) | Live RPC |
| `workspace.index` | ListPanel (Files tab tree) | Live RPC |
| `workspace.preview` | ListPanel + Inspector (selected file preview) | Live RPC |
| `workspace.pinFile` | ListPanel (file row action) | Live RPC |
| `workspace.unpinFile` | ListPanel (file row and pinned action) | Live RPC |
| `workspace.listPinned` | ListPanel + Inspector (Files panels) | Live RPC |
| `workspace.listRecent` | ListPanel + Inspector (Files panels) | Live RPC |
| `context.status` | Inspector (Context tab summary) | Live RPC |
| `context.listBootstrap` | Inspector (Context tab bootstrap list) | Live RPC |
| `context.readBootstrap` | Inspector (Context tab bootstrap preview) | Live RPC |
| `context.listSkills` | Inspector (Context tab skills list) | Live RPC |

The Files tab now uses the Python sidecar's workspace RPCs over the stdio
JSON-RPC transport. The UI can switch the current root through
`workspace.open`, renders backend-provided relative paths from
`workspace.index`, previews files through `workspace.preview`, and sends those
same backend-provided paths to `workspace.pinFile` / `workspace.unpinFile`.
The Inspector Context tab reads `context.status`, `context.listBootstrap`,
`context.readBootstrap`, and `context.listSkills`; the Inspector Files tab is
backed by `workspace.listPinned`, `workspace.listRecent`, and
`workspace.preview`.

The Memory tab is a live operations panel for search, remember/update, daily
notes, lessons, and snapshot management. Successful memory mutations emit
`MemoryChanged`, which refreshes the relevant panels. The Cron tab includes
minimal live job management for `every_ms` and `at_ms` schedules plus heartbeat
status and updates. Successful cron add/update/delete operations emit
`CronJobChanged`. The Inspector Tools tab reads MCP status from `mcp.status`
and has a manual Refresh button for `mcp.status` and `tool.list`; MCP status is
read-only until a stable backend MCP status-change source is wired.

Settings writes use nested `config.write` updates that match the Pydantic config
schema. Dot-path keys such as `agents.defaults.model` are rejected by the
backend. The workspace restriction flag belongs under `tools`, for example:

```json
{
  "updates": {
    "agents": {
      "defaults": {
        "model": "openai/gpt-4o",
        "workspace": "~/projects/example",
        "max_tokens": 4096,
        "temperature": 0.7
      }
    },
    "providers": {
      "openai": {
        "api_key": "sk-...",
        "api_base": "https://api.example.com/v1"
      }
    },
    "tools": {
      "restrict_to_workspace": true
    }
  }
}
```

### Runtime Event Stream

The chat surface and Inspector Activity panel subscribe to structured runtime events emitted by the Python backend. These events drive the chat transcript, tool call progress, approval cards, execution status, and the recent activity stream:

- `RunStarted`, `RunCompleted`, `RunCancelled` — execution lifecycle
- `MessageDelta` — streaming text deltas merged into the current assistant message
- `MessageFinal` — final assistant message content
- `ToolCallStarted`, `ToolProgress`, `ToolResult` — tool execution tracking
- `ApprovalRequested`, `ApprovalResolved` — dangerous command approval flow
- `Error` — backend errors displayed in the UI error bar

Events arrive as method-style JSON-RPC notifications. The notification `method`
is the event type and `params` contains the event fields without a nested
`type` discriminator:

```json
{"jsonrpc":"2.0","method":"RunStarted","params":{"execution_id":"exec-1","session_key":"desktop:default"}}
```

The frontend `subscribeRuntimeEvents()` helper in `chat-state.ts` parses and
dispatches these notifications. It also tolerates the older `runtime_event`
envelope for compatibility, but the Python sidecar and `MockTransport` emit the
method-style contract.

### MVP Deferred Items

The MVP deliberately defers native tray integration, global shortcuts, native
notifications, full MCP server CRUD/configuration UI, installer packaging, and
advanced workspace operations. MCP remains read-only in the desktop UI with a
manual Refresh button for `mcp.status` and `tool.list`; it does not use a fake
`McpStatusChanged` realtime path.

## Goals

- Provide a real desktop app, not a hosted web app.
- Keep the Python MiQi runtime as the source of truth for agent execution.
- Preserve `miqi agent`, `miqi gateway`, Feishu integration, and existing config behavior.
- Make local tools and MCP servers visible and reviewable.
- Support explicit approval for dangerous actions.
- Make session history, workspace files, memory, context, cron, and heartbeat manageable from one place.

## Non-goals

- No cloud sync in the first desktop release.
- No team or multi-user permission model in the first desktop release.
- No hosted web dashboard.
- No mobile app.
- No plugin marketplace.
- No deep version-control or pull-request workflow in the first release.

## Recommended Runtime Shape

MiQi Desktop should use:

- Tauri 2 for the native desktop shell.
- React + TypeScript for the UI.
- A Python MiQi sidecar process for runtime work.
- newline-delimited JSON-RPC over stdio as the default IPC transport.

Tauri uses a system WebView for rendering, but the product is still a local desktop application. It should not require a browser, hosted server, or default localhost HTTP port.

## First-run Experience

The first desktop launch should detect whether `~/.miqi/config.json` exists.

If no usable config exists, the app should guide the user through the same core setup as `miqi onboard`:

- choose a provider;
- enter or validate API credentials;
- choose a default model;
- choose or confirm a MiQi data directory;
- choose or confirm an initial project workspace;
- show optional MCP setup guidance.

The desktop onboarding flow should save through the same config loader and Pydantic schema used by the CLI.

## Main Screens

### Chat

The central screen should support:

- new chat;
- message composer with multiline input;
- active model indicator;
- optional file attachments;
- pinned context chips;
- assistant streaming when supported by the provider;
- tool-call cards;
- MCP progress updates;
- cancel, retry, and regenerate actions;
- clear completion states for success, cancellation, error, and approval denial.

### Sessions

The session view should support:

- list sessions from desktop, CLI, Feishu, cron, heartbeat, and system origins when available;
- create, rename, pin, archive, delete, and export sessions;
- search session messages;
- filter by source, date, model, and tool usage;
- continue an existing CLI or Feishu session when doing so is safe and clear.

### Workspace Files

The workspace view should support:

- selecting a project workspace;
- indexing and showing a file tree;
- showing recent files touched by the agent;
- previewing text files;
- pinning files or directories into context;
- opening the project in the system file manager;
- respecting ignore rules and `restrictToWorkspace`.

### Context Manager

The context manager should explain what the next agent run will see:

- bootstrap files;
- active skills;
- memory hits;
- session history window;
- pinned files;
- attached media;
- context budget and compression state.

The UI may allow include, exclude, pin, unpin, clear, or archive actions, but it must not bypass the core `ContextBuilder` safety assumptions.

### Tools and MCP

The tools view should show:

- built-in tool schemas;
- MCP server status;
- lazy MCP activation state;
- recent tool calls;
- tool timeout and progress settings;
- tool errors;
- approval history for dangerous actions.

Claude Desktop is the primary reference for this area: local tools should be visible and understandable, and sensitive actions should be approved explicitly.

### Memory

The memory view should show:

- memory status;
- long-term snapshot entries;
- self-improvement lessons;
- daily notes;
- session-derived lessons;
- a safe way to explicitly remember selected user-provided facts.

### Cron and Heartbeat

The automation view should show:

- scheduled cron jobs;
- next run time;
- last run status;
- add, update, disable, and delete actions;
- heartbeat enabled state;
- heartbeat interval;
- latest heartbeat result.

### Settings

Settings should cover:

- provider credentials;
- default model;
- temperature and token limits;
- project workspace;
- MiQi data root;
- MCP servers;
- Feishu settings;
- command approval policy;
- session storage mode;
- logs and diagnostics.

Settings writes must go through Pydantic validation and preserve safe file permissions.

## Safety Requirements

- API keys must never be printed in UI logs or IPC diagnostics.
- The default IPC transport must not listen on a network port.
- Dangerous shell commands must trigger explicit approval.
- Denied commands must not execute.
- Project workspace restrictions must be visible and enforced consistently.
- MCP stdio servers should be treated as trusted local code; the UI should make configured commands and env usage visible.

## Compatibility Requirements

Desktop work must not break:

```bash
miqi agent -m "hello"
miqi agent
miqi gateway
miqi memory status
miqi config mcp list
```

## Suggested Milestones

1. Shared runtime factory and desktop backend command.
2. JSON-RPC protocol and structured event stream.
3. Agent execution service with cancellation and approval.
4. Session, workspace, context, memory, tools, MCP, cron, and heartbeat services.
5. Tauri shell and core chat UI.
6. Inspector panels and settings.
7. Packaging and installation checks, beginning with Windows.
