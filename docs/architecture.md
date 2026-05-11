# Architecture

## System Overview

MiQi follows a **message bus + agent loop + tool system + channel adapters** architecture today. A planned desktop entry point extends this into a three-entry architecture: CLI, gateway, and desktop sidecar.

1. **Channels** receive external messages and publish them to `MessageBus.inbound` (Feishu is wired in the packaged gateway today; other adapter modules are extension points in the repository)
2. **AgentLoop** consumes messages, builds context (session + memory + skills), and calls the LLM
3. External capabilities are executed through **ToolRegistry**, either sequentially or concurrently for safe batches
4. Responses are published to `MessageBus.outbound` and delivered back by channels

The desktop direction is documented in [Desktop](desktop.md), [Desktop Architecture](desktop-architecture.md), and [Desktop Design](desktop-design.md). The desktop app should reuse the Python runtime through a sidecar rather than reimplementing agent behavior in the frontend.

```
 ┌─────────────────────────────────────────────────────────┐
 │  Channel Adapters (Feishu / Telegram / Discord / ...)   │
 └─────────────────┬───────────────────────────────────────┘
                   │ InboundMessage
                   ▼
 ┌─────────────────────────────────────────────────────────┐
 │  MessageBus (inbound queue / outbound queue)            │
 └─────────────────┬───────────────────────────────────────┘
                   │
                   ▼
 ┌─────────────────────────────────────────────────────────┐
 │  AgentLoop                                              │
 │    ├── ContextBuilder (session + memory + skills)       │
 │    ├── LLM Provider (OpenAI / Anthropic / ...)         │
 │    └── ToolRegistry (filesystem / shell / web / MCP)   │
 └─────────────────────────────────────────────────────────┘
```

## Core Modules

| Module | File | Responsibility |
|---|---|---|
| Agent loop | `agent/loop.py` | Main loop, tool-call orchestration, MCP lifecycle |
| Context builder | `agent/context.py` | Context assembly: session, memory, skills |
| Runtime controls | `agent/context_compressor.py`, `agent/iteration_budget.py`, `agent/smart_routing.py` | Optional compression/routing hooks plus iteration-pressure safeguards |
| Command approval helper | `agent/command_approval.py` | Interactive dangerous-command approval helper for embedded runtimes |
| Memory store | `agent/memory/store.py` | `MemoryStore` facade over all memory sub-systems |
| Snapshot memory | `agent/memory/snapshot.py` | Long-term RAM-first snapshot with disk checkpoints |
| Lessons | `agent/memory/lessons.py` | Self-improvement lesson extraction and storage |
| NLP helpers | `agent/memory/nlp.py` | Text normalization and relevance scoring |
| LLM providers | `providers/` | Multi-provider adapters unified under a single interface |
| Provider fallback helper | `providers/fallback.py` | Retry/fallback chain helper for advanced embeddings |
| Channel adapters | `channels/` | IM and messaging platform adapters |
| Tool registry | `agent/tools/registry.py` | Tool registration, discovery, and dispatch |
| Built-in tools | `agent/tools/` | `filesystem`, `shell`, `web`, `papers`, `cron`, `spawn`, `message` |
| Cron service | `cron/service.py` | Scheduled task execution engine |
| Session manager | `session/manager.py` | Default JSONL session persistence and compaction |
| SQLite session backend | `session/sqlite_store.py` | Optional SQLite+FTS5 backend module shipped in the repository |
| CLI | `cli/` | Entry point and subcommand modules |

## Planned Desktop Entry Point

MiQi Desktop is planned as a local desktop application built around a Tauri shell and a Python sidecar process. It should add a new runtime entry point without replacing the existing CLI or gateway.

```
Desktop Window (Tauri + React)
          │ stdio NDJSON JSON-RPC
          ▼
miqi desktop-backend --stdio
          │
          ▼
Application services
          ├── AgentService / ExecutionManager
          ├── SessionService
          ├── WorkspaceService
          ├── ContextService
          ├── MemoryService
          ├── ToolApprovalService
          └── Tool / MCP / Cron / Heartbeat services
          │
          ▼
Existing AgentLoop + providers + tools + memory
```

The desktop backend should expose structured JSON-RPC methods and structured runtime events. The existing string-oriented `on_progress` hook can remain for CLI/gateway compatibility, while desktop consumers subscribe to event types such as `RunStarted`, `MessageDelta`, `ToolCallStarted`, `ApprovalRequested`, `SessionChanged`, and `Error`.

The desktop implementation should also separate the user's **project root** from the MiQi **data root**. Existing installations can continue using `agents.defaults.workspace` for both until a separate project root is configured.

## Data Flow

```
Input:   Channel adapter → InboundMessage (with sender identity, channel metadata)
          ↓
Process: AgentLoop.process_*
          ├── ContextBuilder assembles: session history + memory items + lessons + skill files
          ├── LLM Provider generates response (streaming or blocking)
          └── If tool calls: ToolRegistry dispatches (sequentially or concurrently) → tool executes → result fed back
          ↓
Output:  OutboundMessage → Channel adapter delivers to user
          ↓
Memory:  MemoryStore.record_turn updates short-term ring buffer and long-term snapshot
```

## Key Design Principles

- **Workspace-local runtime data**: memory and session files live under `agents.defaults.workspace` by default (`~/.miqi/workspace/`), not the config root itself.
- **RAM-first memory**: runtime operates entirely from in-memory structures; disk writes happen only at checkpoint events. See [Memory System](memory-system.md).
- **Tool registry**: decouples tool definitions (schema), validation, and execution; MCP tools and built-in tools share the same dispatch interface.
- **Safe concurrency**: `ToolRegistry` parallelizes read-only or path-disjoint batches to reduce round-trip latency without reordering stateful tools.
- **Provider abstraction**: all LLM providers implement a unified `BaseProvider` interface; the agent loop calls `provider.chat(messages, tools)` without knowing which backend is in use.
- **Iteration pressure control**: `IterationBudget` injects hints as the loop nears `maxToolIterations`, reducing runaway tool cycles.
- **Session storage**: `SessionManager` rewrites JSONL session files periodically to keep context size bounded while preserving readable history. The repository also ships an optional SQLite+FTS5 backend module, but the packaged CLI/gateway path still instantiates JSONL sessions.
- **Embedded advanced helpers**: smart model routing, provider fallback, command approval, and context compression are present as runtime modules; only the always-safe pieces are active in the packaged CLI/gateway defaults today.
- **Desktop as a third entry point**: desktop work should extract shared runtime construction and application services while preserving the CLI and gateway behavior.
- **Structured events for UI**: desktop needs typed events for streaming, tool state, approvals, cancellation, session changes, memory changes, and errors. CLI/gateway can keep rendering string progress.
- **Explicit approval surface**: dangerous local actions should be visible and user-approved in desktop, using `agent/command_approval.py` as the detection source of truth.

## Group Chat Message Filtering

Feishu (and other group-capable channels) supports @mention filtering:

- When `channels.feishu.requireMentionInGroups` is `true` (default), group chat messages are only forwarded to the agent if the bot is explicitly @mentioned.
- Private (p2p) chats always pass through.
- The @mention placeholder is stripped from the message text before it reaches the agent.

## Task Queue and User Notifications

`AgentLoop` processes messages serially. When busy, new messages enter a pending queue managed by `TaskTracker`:

- Senders receive a queue position notification (e.g. "You are #2 in queue").
- When a task starts, the sender is notified.
- CLI and system messages bypass the queue.
- Controlled by `channels.sendQueueNotifications` config (default `true`).

## MCP Tool Progress Reporting

Long-running MCP tool calls support two kinds of progress feedback:

1. **SDK progress** — if the MCP server sends progress events, they are forwarded to the user as percentage updates.
2. **Heartbeat** — `MCPToolWrapper` starts a background timer that sends elapsed-time messages every `progressIntervalSeconds` (default 15s), independent of the MCP server. The heartbeat is cancelled when the tool returns.

## CLI Architecture

| File | Role |
|---|---|
| `cli/commands.py` | Entry point and compatibility exports |
| `cli/onboard.py` | Onboarding command |
| `cli/agent_cmd.py` | `miqi agent` command |
| `cli/gateway_cmd.py` | `miqi gateway` command |
| `cli/management.py` | channels / memory / session / cron / status / provider commands |
| `cli/config_cmd.py` | `miqi config` subcommands |
