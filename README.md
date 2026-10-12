# Codex Telegram Plugin

Control Codex sessions through a private Telegram bot without storing live
credentials in the plugin checkout. The plugin supports a shared external
Telegram receiver and a standalone Python receiver.

## Installation

See [INSTALL.md](INSTALL.md) for agent-assisted and manual installation.

To have a coding agent perform the installation, send it this prompt:

```text
Install and validate the Codex Telegram plugin from
https://github.com/Bing72/codex-telegram-plugin by following INSTALL.md.

Before changing anything, inspect the current Codex, installed Hermes/profile
(if present), external Telegram receiver, and user-service configuration. Show
me the proposed mode, commands, affected files (including Hermes's plugin
folder and approval transport), and service impact, then wait for my
confirmation. Never print or copy live Telegram credentials into the repository,
logs, or plugin source files. Do not commit, push, or expose a local bridge
port unless I explicitly ask. On updates, rerun `scripts/setup.py --refresh`
(after inspecting the existing mode) to refresh the Hermes adapter too.
```

## Features

- Final-answer completion and interruption notifications through Codex lifecycle hooks
- Stable randomized emoji markers that distinguish concurrent Codex sessions
- Telegram approval buttons for Codex permission requests
- Bundled MCP `ask_user` tool that mirrors option and custom-text questions to Telegram and supported Codex clients
- Session titles and question state mirrored into the active Codex session
- `/sessions`, `/status N`, and `/start_work N` session control
- Loopback-only session bridge backed by `codex app-server --stdio`
- Optional Hermes adapter installed by `scripts/setup.py` when Hermes is available:
  labeled completion/interruption notifications, a Telegram question tool, and
  a human approval transport using the same bot (no second polling consumer)

## Runtime modes

| Mode | Telegram receiver | Credential behavior |
| --- | --- | --- |
| `shared` | Existing external Telegram receiver | References an existing credential file; never copies the token into this checkout |
| `standalone` | Bundled Python `getUpdates` poller | Uses a credential file outside this checkout |

Both receivers use the same token-scoped lock. Only one process can poll a bot
token at a time.

## Telegram commands

- `/sessions`: up to 20 recent root sessions
- `/status N`: current state and recent user/assistant messages
- `/start_work N`: safely resumes an idle Codex thread
- `/help`: command help

## Question and approval routing

While the user is active in the Codex session, agents should prefer Codex's
native question UI or one concise in-session question. When `ask_user` is
invoked, clients that negotiate MCP form elicitation receive the same question
and Telegram receives inline buttons. The first completed answer wins; the
other channel is closed or cancelled. The MCP server negotiates the client's
form-elicitation protocol (2025-06-18, 2025-11-25, or 2026-07-28), using each
version's choice schema. Multi-select questions on the 2025-06-18 protocol
remain Telegram-only because that form schema cannot represent multi-select.
Clients without form elicitation support keep the Telegram-only fallback.

To send selectable Codex questions to Telegram even while the local session is
open, set `"question_routing": "telegram_choices"` in the local
`~/.config/codex-telegram-plugin/config.json`. The SessionStart hook then
directs agents to use `ask_user` for choice questions and fall back to native
questions if Telegram delivery fails. Free-text questions remain native-first.
This routes agent-initiated questions; it does not intercept arbitrary Codex
CLI dialogs or change approval handling. Existing open sessions receive the
same routing guidance on their next user prompt; an already-running turn is
not changed. The setting survives plugin setup.

Telegram is also the response channel for plugin-managed approval prompts. If a
Telegram approval fails or times out, the hook returns no decision and Codex
falls back to its native approval flow.

For ordinary local Codex sessions, only final answers are sent to Telegram with
a visible `✅` completion marker. When Codex 0.150.0 or later interrupts an
active top-level turn, the `Interrupt` hook sends a visible `🚨` Telegram alert
instead. Each session keeps one randomized emoji marker across both notification
types, making interleaved messages from concurrent sessions easier to distinguish.
The original Markdown remains unchanged for the Codex TUI. A separate Telegram
copy is converted deterministically to supported Telegram HTML and split at safe
message boundaries without any additional LLM or model call.

## Hermes integration (when installed)

`setup.py` discovers `hermes` on `PATH`, installs the adapter in the active
profile's `plugins/codex-telegram-bridge/`, enables it, and selects the
`codex-telegram` approval transport if the existing transport is `builtin`.
It does not overwrite another configured transport or start Hermes's own
Telegram gateway. Approval delivery failure, timeout, or an invalid reply
**denies** the operation rather than granting it. The plugin reuses the
configured credential reference and the existing Telegram question receiver;
only one process polls a bot token. Restart an existing Hermes process after
installation or update so it loads the refreshed plugin.

Hermes completion and interruption notifications are visibly labeled `Hermes`.
Their headings include the current Hermes session title, just like Codex
notifications (for example, `✅ Hermes 완료 · Telegram 제목 구현`). Questions
use the same title. The adapter reads it from the active profile's `state.db`
in read-only mode on each send, so renamed sessions are reflected without a
model call. If no title is available, it falls back to `Hermes · <session-id>`.
The `hermes_telegram_ask_user` tool sends choice and free-text questions to
the plugin's bot and returns the selected answers to Hermes; on delivery
failure it returns an error telling the agent to use native `clarify`. The
adapter gives Hermes guidance to use this tool, but it does **not** silently
intercept every native `clarify` invocation. Hermes dangerous-command approvals
use its host-owned approval transport and preserve its allowed scopes and safety
checks. Codex `/sessions`, `/status`, and `/start_work` remain Codex-only.

For subsequent repository updates, refresh both integrations in one step:

```bash
python3 scripts/setup.py --refresh
```

Use `--install-services` only when intentionally restarting the applicable
systemd user services. The refresh reuses the existing mode, credential file,
and receiver; it never creates a second polling consumer.

## Security and local data

- Live credentials, generated services, runtime state, caches, and logs are
  ignored by Git and remain outside the tracked source tree.
- The bridge accepts only loopback binds and loopback HTTP callback URLs.
- Pending/session files use mode `0600`; their directories use `0700`.
- User-visible Telegram text and default runtime errors pass through a
  credential and private-path redaction boundary.
- A `/proc` file-descriptor check prevents resuming a rollout already open in
  another Codex process.

The local bridge is a same-host trust boundary, not a public API. Do not expose
it through a public bind, proxy, tunnel, or port mapping. See
[SECURITY.md](SECURITY.md). Telegram bot chats are not end-to-end encrypted, so
do not send secrets or regulated data through questions, status messages,
completions, or approvals.

## License

MIT. See [LICENSE](LICENSE).
