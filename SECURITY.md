# Security policy

## Reporting a vulnerability

Use the repository's private **Report a vulnerability** flow (GitHub Security
Advisories). Do not post live bot tokens, chat IDs, user IDs, private paths, or
session content in a public issue.

If a credential may have been exposed, revoke or rotate it before submitting a
report. Redacting a Git commit does not invalidate an already exposed secret.

## Runtime trust boundary

- Keep Telegram credentials outside the checkout in a mode `0600` file.
- Keep `TELEGRAM_ALLOWED_USER_IDS` restricted to the intended private user.
- Keep the Codex bridge bound to loopback only. Do not publish its port through
  a reverse proxy, container port mapping, SSH remote forward, or public bind.
- Treat every process that can connect to the local bridge as trusted: the
  bridge can read session summaries and request an idle session resume.
- Review Telegram permission prompts before approving them; the bot is an
  approval transport, not an independent authorization policy.
- Telegram bot chats are not end-to-end encrypted. Do not route secrets or
  regulated data through question, status, completion, or approval messages.
