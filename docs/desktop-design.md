# Desktop Design

MiQi Desktop should feel like a focused desktop control center for a local AI agent. It should borrow the calm chat and explicit tool approval patterns of Claude Desktop, plus the task/thread/workspace command-center feel of Codex app. It should not look like a marketing website.

!!! note "Implementation status"
    This is the target design brief for the desktop implementation. It should guide component structure, layout, color tokens, and interaction behavior.

## Design Principles

- Put the working surface first. The first screen should be usable chat/workspace UI, not a landing page.
- Keep the interface dense but readable.
- Make local capability visible: tools, MCP servers, files, memory, cron, and context should not be hidden in logs.
- Treat approvals as a first-class interaction, not a terminal prompt translated into a modal at the last second.
- Keep navigation stable. Users should always know which workspace, session, model, and execution state are active.
- Prefer restrained desktop patterns over decorative product-page patterns.

## Layout

Use a four-region desktop layout:

```
Rail | List Panel | Chat Surface | Inspector
```

### Rail

The rail is a narrow icon-first navigation strip.

Primary items:

- Workspace
- Chats
- Files
- Tools
- Memory
- Cron
- Settings

Use tooltips for icon-only controls. Use badges for active runs, pending approvals, failed MCP servers, or scheduled-job alerts.

### List Panel

The list panel changes based on the rail selection.

For Chats it should show:

- search;
- new chat button;
- active/pinned sessions;
- recent sessions;
- source/date/model filters.

For Files it should show:

- workspace selector;
- file tree;
- recent files;
- pinned context.

### Chat Surface

The central surface is the primary work area.

It should contain:

- current session title and model indicator;
- transcript;
- tool and approval cards inline with messages;
- streaming assistant response;
- composer with context chips and attachment affordances;
- cancel/retry/regenerate controls.

### Inspector

The right inspector should be collapsible and tabbed.

Tabs:

- Context
- Activity
- Files
- Memory
- Tools

The inspector should be informational and actionable, but it should not feel like a second app embedded beside chat.

## Chat Components

### User Message

- Compact bubble or unframed block, depending on density.
- Attachments shown as chips or rows.
- Timestamp available on hover or in a subtle metadata row.

### Assistant Message

- Markdown rendering.
- Streaming delta state.
- Final metadata row for model, elapsed time, and tool count.
- Actions: copy, retry, regenerate, remember selected text when safe.

### Tool Card

Tool cards should show:

- tool name;
- status: pending, running, completed, failed, cancelled;
- compact argument summary;
- progress if available;
- expandable result preview;
- full output only when the user expands.

### Approval Card

Approval cards should be inline and hard to miss.

Required elements:

- tool name;
- command or action summary;
- matched danger reason;
- affected path or workspace when available;
- buttons: Deny, Approve Once, Approve Session, Approve Always;
- timeout or stale state if applicable.

Denied actions must remain visible in the transcript/activity log.

## Workspace and Context UX

Workspace file interactions should make agent context concrete.

Required patterns:

- file tree with stable row heights;
- recent files list;
- pin/unpin context action;
- context chips in composer;
- context budget meter;
- source labels for memory, skills, session history, pinned files, and attachments.

Do not let the user assume pinning a file bypasses tool safety or workspace restrictions.

## Color System

Use the user-provided Microscopic Era brand palette.

| Token | Hex | RGB | Role |
|---|---|---|---|
| `brand-yellow` | `#F2D760` | `242, 215, 96` | Primary action, active state, selected state |
| `brand-red` | `#DB5F5B` | `219, 95, 91` | Destructive action, warning, denied approval, failed status |
| `brand-indigo` | `#2B3150` | `43, 49, 80` | Navigation, structural emphasis, dark shell |
| `neutral-900` | `#333333` | `51, 51, 51` | Primary text, high-contrast icons |
| `neutral-0` | `#FFFFFF` | `255, 255, 255` | Raised surfaces, inputs, modal bodies |
| `warm-canvas` | `#F5F6E5` | `245, 246, 229` | Main light background |

### Light Theme

Suggested mappings:

| Semantic token | Value |
|---|---|
| `--color-bg` | `#F5F6E5` |
| `--color-surface` | `#FFFFFF` |
| `--color-surface-subtle` | `#F7F7EC` |
| `--color-text` | `#333333` |
| `--color-text-muted` | `rgba(51, 51, 51, 0.68)` |
| `--color-border` | `rgba(51, 51, 51, 0.18)` |
| `--color-nav` | `#2B3150` |
| `--color-primary` | `#F2D760` |
| `--color-danger` | `#DB5F5B` |

### Dark Theme

Suggested mappings:

| Semantic token | Value |
|---|---|
| `--color-bg` | `#20243A` |
| `--color-surface` | `#2B3150` |
| `--color-surface-subtle` | `#333A5E` |
| `--color-text` | `#FFFFFF` |
| `--color-text-muted` | `rgba(255, 255, 255, 0.72)` |
| `--color-border` | `rgba(255, 255, 255, 0.16)` |
| `--color-primary` | `#F2D760` |
| `--color-danger` | `#DB5F5B` |

Rules:

- Use `brand-yellow` for primary actions and active state. Do not wash the entire app in yellow.
- Use `brand-red` only for danger, failure, warning, and denial.
- Use `brand-indigo` for structure and navigation, not as the only color in the UI.
- Use `warm-canvas` as a light background, balanced with white surfaces.
- Avoid dominant purple, blue-purple gradients, beige-only themes, brown/orange themes, or a one-hue palette.
- Do not use decorative gradient blobs or orb backgrounds.

## Shape and Spacing

- Prefer 4px to 8px border radius.
- Avoid nested cards.
- Page sections should be unframed regions or panels, not floating marketing cards.
- Use stable dimensions for rails, tool rows, message controls, and list rows.
- Avoid viewport-scaled typography.
- Letter spacing should remain `0` except for tiny labels if absolutely necessary.

## Typography

- Use system UI fonts by default.
- Keep compact headings inside panels.
- Reserve large display text for empty states only, and even then keep it restrained.
- Do not explain the UI inside the UI with long instructional copy.

## Iconography

- Prefer lucide icons when the frontend stack supports them.
- Use icons for rail navigation, tool buttons, file actions, approve/deny status, and settings categories.
- Add accessible labels and tooltips for icon-only buttons.

## States

Every major panel should handle:

- empty state;
- loading state;
- ready state;
- running state;
- waiting for approval;
- error state;
- disconnected sidecar state;
- cancelled state.

Sidecar disconnection should be visible but not alarming if the app can restart it.

## Accessibility

- Keyboard navigation should cover rail, session list, composer, inspector tabs, and approval actions.
- Do not rely on color alone for status.
- Keep contrast high enough on `brand-yellow` by pairing it with `neutral-900` text.
- Destructive actions should require clear labels.

## Copy Tone

Use concise desktop-product copy:

- "Approve once"
- "Approve for session"
- "Deny"
- "Cancel run"
- "Pinned context"
- "MCP server disconnected"

Avoid marketing phrases, hype, and tutorial text embedded in the main interface.
