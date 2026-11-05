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

Only one process may poll a bot token at a time. Shared mode requires an
operator-managed receiver that implements this plugin's pending-question and
permission protocols and uses the loopback bridge for session control. Merely
sharing credentials does not integrate an arbitrary receiver. Setup does not
install or manage that receiver.
Cooperating receivers must use the same token-scoped polling lock.

## Telegram commands

- `/sessions`: up to 20 recent root sessions
- `/status N`: current state and recent user/assistant messages
- `/start_work N`: safely resumes an idle Codex thread
- `/help`: command help

## Question and approval routing

By default, while the user is active in the Codex session, agents should prefer Codex's
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
not changed. The rule also covers selectable questions that would otherwise
use `request_user_input_async` in newer Codex versions. The setting survives
plugin setup.

If questions stop arriving, verify that the credential file referenced by
`env_path` still exists and run `python3 scripts/setup.py --refresh --test-connection`.
In standalone mode, `codex-telegram-poller.service` must be active to receive
answers; the bridge service alone does not poll Telegram. Restore a moved
credential reference with `--mode standalone --env-file /absolute/path/to/existing.env`
before starting the standalone poller. Never run a second polling consumer for
the same bot.

Telegram is also the response channel for plugin-managed approval prompts. If a
Telegram approval fails or times out, the hook returns no decision and Codex
falls back to its native approval flow.

| Input | Hermes | Codex |
| --- | --- | --- |
| Single choice, free text, and custom answers | Native `clarify` and `hermes_telegram_ask_user` mirror to Telegram and an attached terminal | `ask_user` mirrors to Telegram and clients with MCP form elicitation; configured choice routing guides native question calls to this tool |
| Multiple selection, including custom text | Supported | Supported through `ask_user`; legacy clients without array-form support use Telegram |
| Questions in one batch | Up to 5, matching native Hermes `clarify` | Up to 3 through `ask_user` |
| Execution, file, network, and permission approvals | Hermes's host-bound approval transport and allowed choices | Permission hooks or the app-server bridge; only offered approval scopes are displayed and accepted |
| Passwords, API keys, sudo/vault unlock, login and OAuth | Native secure input and browser flows | Native secure input and browser flows; `isSecret` questions are never forwarded by the bridge |
| Third-party MCP forms and built-in CLI menus | Their owning native client | Their owning native client; the session bridge does not implement arbitrary MCP schemas or URL authentication flows |

Hermes native `clarify` mirroring uses tool-execution middleware when the host
supports it, and keeps the native `responses` and `outcome` contract. Other tools
and approval gates continue through their host handlers. Automatic mirroring
requires an owned interactive terminal; finite/headless turns, subagents without
their own viewer, and messaging-native questions retain their host input policy.
The explicit Hermes Telegram tool can still ask remotely without a local viewer. Native question
cancellation closes the mirrored wait. Declining a Codex MCP form leaves the
Telegram answer path available; cancelling the question or its MCP call stops
both. App-server resolution or disconnect retires that connection's pending
question/approval, and late replies cannot answer a replacement connection.

For ordinary local Codex sessions, only final answers are sent to Telegram with
a visible `✅` completion marker. Subagent completions are excluded in both
Codex lifecycle hooks and the app-server bridge; main-agent completions remain
enabled. Hooks also check the rollout header when older Codex versions omit
the subagent identifier. When Codex 0.150.0 or later interrupts an
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
Completion notifications from Hermes subagents are suppressed; the main agent's
completion notification, questions, approvals, and interruption behavior are unchanged.
Their headings include the current Hermes session title, just like Codex
notifications (for example, `✅ Hermes 완료 · Telegram 제목 구현`). Questions
use the same title. The adapter reads it from the active profile's `state.db`
in read-only mode on each send, so renamed sessions are reflected without a
model call. If no title is available, it falls back to `Hermes · <session-id>`.
The `hermes_telegram_ask_user` tool sends choice and free-text questions to
the plugin's bot. In an interactive Hermes CLI, it also opens Hermes's native
terminal question panel: either channel can submit, and the first submission
wins. Terminal submissions cancel the Telegram wait; Telegram submissions
close the terminal panel. Multi-select and free-text answers work on both.
For gateway-backed Hermes CLI sessions, questions use the owning session's
native clarify request, so the terminal panel appears even though the tool runs
in the gateway. Replies and cancellation stay bound to that session and profile.
If Telegram delivery fails, the terminal panel remains usable. In contexts
without an interactive CLI, delivery failure returns an error telling the
agent to use native `clarify`, together with the failed stage and safe diagnostic
details. Failures are also logged without raw unexpected-exception messages.
A missing configured credential file is reported explicitly; restore its
reference or rerun setup with the correct `--env-file`. Telegram initialization
failures do not prevent an interactive terminal panel from opening. The
adapter also mirrors ordinary native `clarify` calls through Hermes's supported
tool-execution middleware, retaining native validation, results, and cancellation.
Hermes dangerous-command approvals
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
- Pending files use mode `0600`; their directories use `0700`.
- Pending questions and permissions use `codex-telegram-pending-questions-<token_hash>`
  and `codex-telegram-pending-permissions-<token_hash>` directories under
  Python's `tempfile.gettempdir()`. The token-scoped polling lock is
  `codex-telegram-<token_hash>.lock` in the same temporary directory.
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
