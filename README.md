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

Before changing anything, inspect the current Codex, external Telegram
receiver, and user-service configuration. Show me the proposed mode, commands,
affected files, and service impact, then wait for my confirmation. Never print
or copy live Telegram credentials into the repository, logs, or plugin source
files. Do not commit, push, or expose a local bridge port unless I explicitly
ask.
```

## Features

- Final-answer completion and interruption notifications through Codex lifecycle hooks
- Stable randomized emoji markers that distinguish concurrent Codex sessions
- Telegram approval buttons for Codex permission requests
- Bundled MCP `ask_user` tool that mirrors option and custom-text questions to Telegram and supported Codex clients
- Session titles and question state mirrored into the active Codex session
- `/sessions`, `/status N`, and `/start_work N` session control
- Loopback-only session bridge backed by `codex app-server --stdio`

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
other channel is closed or cancelled. Clients without elicitation support keep
the existing Telegram-only fallback.

Telegram is also the response channel for plugin-managed approval prompts. If a
Telegram approval fails or times out, the hook returns no decision and Codex
falls back to its native approval flow.

For ordinary local Codex sessions, only final answers are sent to Telegram with
a visible `✅` completion marker. When Codex 0.150.0 or later interrupts an
active top-level turn, the `Interrupt` hook sends a visible `🚨` Telegram alert
instead. Each session keeps one randomized emoji marker across both notification
types, making interleaved messages from concurrent sessions easier to distinguish.

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
