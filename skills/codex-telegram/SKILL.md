---
name: codex-telegram
description: Use the installed Telegram bridge when Codex needs a real user answer, approval, completion notification, or remote session continuation.
---

# Codex Telegram

## Rules

1. While the user is present in the current Codex session, prefer its native
   question surface or ask one concise in-session question and wait for the
   local answer. Use the `codex-telegram` MCP server's `ask_user` tool only when
   the current session cannot receive the answer or the user explicitly wants
   a remote Telegram response.
2. Provide one to three concise questions. Prefer two or three mutually
   exclusive options and keep custom input enabled when free text is useful.
3. Never include tokens, credentials, private keys, or full secret-bearing
   command output in Telegram questions or notifications.
4. Permission prompts are handled automatically by the plugin hook or Codex
   app-server bridge. Never create a second polling consumer for the same bot.
5. `/sessions`, `/status N`, and `/start_work N` are served by the configured
   receiver: an external Telegram receiver in shared mode or the bundled
   poller in standalone mode.
6. `ask_user` publishes its pending question through MCP progress so the
   current Codex session can render it, and also mirrors it to the controlling
   TTY when one exists. Telegram remains the authoritative response channel for
   an `ask_user` call. Permission decisions keep their existing single-channel
   safety rule because a deciding `PermissionRequest` hook suppresses Codex's
   separate native approval prompt.

## Success criteria

- The user answer returned by `ask_user` is used as authoritative input.
- A denied permission remains denied.
- The current Codex session shows the `ask_user` question through MCP progress
  when the client supplies a progress token; a controlling TTY remains a
  fallback mirror.
- Session resume is attempted only for an idle Codex thread that is not open in
  another Codex process.
