# Installation

This guide supports agent-assisted installation and direct manual setup. Keep
all live Telegram credentials outside the repository.

## Agent-assisted installation

Copy and send the following prompt to a coding agent:

```text
Install and validate the Codex Telegram plugin from:
https://github.com/Bing72/codex-telegram-plugin

Follow these requirements:

1. Read INSTALL.md and SECURITY.md before making changes.
2. Inspect the current Codex installation, plugin support, existing external
   Telegram receiver, credential-file references, and relevant user services.
3. Before changing anything, report:
   - the proposed runtime mode (`shared` or `standalone`),
   - every command you intend to run,
   - the files and user services that will change,
   - whether a service will be installed, enabled, disabled, or restarted.
   Then wait for my confirmation.
4. Never print, echo, log, commit, upload, or copy a live bot token, chat ID, or
   allowed-user ID into the repository or plugin source files.
5. If an external Telegram receiver is available, prefer `shared` mode and
   reference its credential file without copying it. Keep exactly one Telegram
   `getUpdates` consumer active.
6. If there is no existing receiver, propose `standalone` mode. Request only
   the missing values through secure input and store them outside the checkout
   in a user-owned mode `0600` file.
7. Clone the repository, add its Codex marketplace, install the plugin, and run
   `scripts/setup.py` with the selected mode. Use `--install-services` only on
   Linux systems that support `systemd --user`.
8. Keep the Codex bridge bound to loopback. Do not expose it through a public
   bind, reverse proxy, container port mapping, SSH remote forward, or tunnel.
9. After installation, verify the test suite, manifests, plugin registration,
   applicable user services, loopback health endpoint, Telegram connection,
   and the absence of credentials in tracked or installed package files.
10. Start a new Codex process after installation so the current plugin cache,
    hooks, and MCP launcher are loaded.
11. Do not commit, push, delete unrelated files, or change unrelated services
    unless I explicitly request it.
```

## Requirements

- Git
- Codex CLI with plugin support
- Python 3.10 or newer
- Linux user services (`systemd --user`) only when using
  `--install-services`

## Clone and install the plugin

```bash
git clone https://github.com/Bing72/codex-telegram-plugin.git
cd codex-telegram-plugin

codex plugin marketplace add .
codex plugin add codex-telegram-plugin@codex-telegram
```

Confirm that Codex sees the plugin:

```bash
codex plugin list
```

## Choose a runtime mode

| Mode | Use when | Telegram receiver |
| --- | --- | --- |
| `shared` | An external receiver already owns the bot and polls Telegram | Existing external receiver |
| `standalone` | No existing Telegram receiver is available | Bundled Python poller |

Do not run two `getUpdates` consumers for the same bot token.

## Shared mode

Reference the external receiver's existing credential file without copying its
contents:

```bash
python3 scripts/setup.py \
  --mode shared \
  --env-file /path/to/telegram.env \
  --install-services \
  --test-connection
```

Shared mode enables the Codex bridge and disables the bundled standalone
poller. The external receiver remains operator-managed.

## Standalone mode

Create a credential file through a non-echoing prompt:

```bash
python3 scripts/setup.py \
  --mode standalone \
  --configure-token \
  --install-services \
  --test-connection
```

The generated credential directory uses mode `0700`, and the credential file
uses mode `0600`.

To reference an existing credential file instead:

```bash
python3 scripts/setup.py \
  --mode standalone \
  --env-file /path/to/telegram.env \
  --install-services \
  --test-connection
```

The credential file must contain:

```dotenv
TELEGRAM_BOT_TOKEN=<BOTFATHER_TOKEN>
TELEGRAM_CHAT_ID=<PRIVATE_CHAT_ID>
TELEGRAM_ALLOWED_USER_IDS=<COMMA_SEPARATED_USER_IDS>
```

On POSIX systems, a referenced credential file must be owned by the current
user and must not be accessible to the group or other users.
`TELEGRAM_ALLOWED_USER_IDS` is mandatory and must include the configured
private chat ID; startup fails closed when it is missing.

## Systems without user systemd

Omit `--install-services` when `systemd --user` is unavailable:

```bash
python3 scripts/setup.py \
  --mode standalone \
  --env-file /path/to/telegram.env \
  --test-connection
```

The command writes the plugin runtime configuration but does not install a
process manager. Start and supervise the bridge and, in standalone mode, the
poller using the service manager appropriate for the operating system.

## Environment-only configuration

Direct process launches may provide:

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
TELEGRAM_ALLOWED_USER_IDS
CODEX_TELEGRAM_MODE=standalone
```

Optional paths and runtime settings:

```text
CODEX_TELEGRAM_CONFIG
CODEX_TELEGRAM_ENV
CODEX_TELEGRAM_SHARED_DIR
CODEX_TELEGRAM_DATA_DIR
CODEX_TELEGRAM_BRIDGE_HOST
CODEX_TELEGRAM_BRIDGE_PORT
CODEX_TELEGRAM_TERMINAL_MIRROR
```

Set `CODEX_TELEGRAM_TERMINAL_MIRROR=0` for a headless process that should not
write terminal mirror events.

## Verification

Validate the checkout:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q scripts tests
python3 -m json.tool .codex-plugin/plugin.json >/dev/null
python3 -m json.tool .agents/plugins/marketplace.json >/dev/null
```

On Linux installations that use user services, inspect the services applicable
to the selected mode:

```bash
systemctl --user status codex-telegram-bridge.service
systemctl --user status codex-telegram-poller.service
```

Verify the default loopback health endpoint:

```bash
curl --fail --silent --show-error http://127.0.0.1:43991/healthz
```

Shared mode should leave `codex-telegram-poller.service` disabled because the
external receiver owns polling. Standalone mode should leave the bundled
poller enabled when user services are installed. Finally, start a new Codex
process and confirm that the plugin, hooks, and `codex-telegram-mcp` launcher
load from the current installation.

## Security notes

- Never place a live credential file inside this checkout.
- Never expose the loopback bridge to another host or network.
- Restrict `TELEGRAM_ALLOWED_USER_IDS` to the intended private user.
- Treat every process that can connect to the bridge as trusted.
- Telegram bot chats are not end-to-end encrypted; do not send secrets or
  regulated data through questions, status messages, completions, or
  approvals.
- See [SECURITY.md](SECURITY.md) for the complete trust boundary and reporting
  instructions.
