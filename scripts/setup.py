#!/usr/bin/env python3
"""Configure shared or standalone Codex Telegram installations."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from common import (  # noqa: E402
    DEFAULT_PLUGIN_CONFIG_DIR,
    DEFAULT_PLUGIN_ENV_PATH,
    DEFAULT_SETTINGS_PATH,
    LEGACY_OPENCODE_CONFIG_DIR,
    LEGACY_OPENCODE_ENV_PATH,
    TelegramBridgeError,
    _telegram_api,
    load_telegram_config,
    redact_sensitive_text,
)


def atomic_write_text(path: Path, text: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def install_launchers() -> None:
    python = shutil.which("python3") or sys.executable
    bin_dir = Path.home() / ".local/bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        bin_dir / "codex-telegram-mcp",
        (
            "#!/bin/sh\n"
            f"exec {systemd_quote(python)} "
            f"{systemd_quote(PLUGIN_ROOT / 'scripts/mcp_server.py')} \"$@\"\n"
        ),
        0o755,
    )


def write_settings(
    *,
    mode: str,
    env_path: Path,
    shared_dir: Path,
    receiver: str,
) -> None:
    payload = {
        "version": 1,
        "mode": mode,
        "env_path": str(env_path),
        "shared_dir": str(shared_dir),
        "receiver": receiver,
        "terminal_mirror": True,
    }
    atomic_write_text(
        DEFAULT_SETTINGS_PATH,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        0o600,
    )


def configure_credentials() -> Path:
    token = getpass.getpass("Telegram BotFather token: ").strip()
    chat_id = input("Private chat ID: ").strip()
    allowed = input(
        f"Allowed Telegram user IDs (comma separated) [{chat_id}]: "
    ).strip() or chat_id
    if not token or not chat_id or not allowed:
        raise TelegramBridgeError("token, chat ID, and allowed user IDs are required")
    try:
        int(chat_id)
        [int(item.strip()) for item in allowed.split(",") if item.strip()]
    except ValueError as exc:
        raise TelegramBridgeError("chat and user IDs must be integers") from exc
    atomic_write_text(
        DEFAULT_PLUGIN_ENV_PATH,
        (
            f"TELEGRAM_BOT_TOKEN={token}\n"
            f"TELEGRAM_CHAT_ID={chat_id}\n"
            f"TELEGRAM_ALLOWED_USER_IDS={allowed}\n"
        ),
        0o600,
    )
    return DEFAULT_PLUGIN_ENV_PATH


def systemd_quote(value: str | Path) -> str:
    return shlex.quote(str(value))


def service_environment_path(*executables: str) -> str:
    paths = [str(Path(item).parent) for item in executables if item]
    paths.extend(["/usr/local/bin", "/usr/bin", "/bin"])
    return ":".join(dict.fromkeys(paths))


def bridge_unit(python: str, codex: str) -> str:
    path = service_environment_path(codex, python)
    return f"""[Unit]
Description=Codex Telegram session and approval bridge
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={systemd_quote(python)} {systemd_quote(PLUGIN_ROOT / "scripts/bridge.py")}
Environment=CODEX_BIN={codex}
Environment=PATH={path}
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
Restart=on-failure
RestartSec=3
UMask=0077
NoNewPrivileges=true

[Install]
WantedBy=default.target
"""


def poller_unit(python: str) -> str:
    return f"""[Unit]
Description=Codex standalone Telegram polling host
After=network-online.target codex-telegram-bridge.service
Wants=network-online.target
Requires=codex-telegram-bridge.service

[Service]
Type=simple
ExecStart={systemd_quote(python)} {systemd_quote(PLUGIN_ROOT / "scripts/poller.py")}
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
Restart=on-failure
RestartSec=3
UMask=0077
NoNewPrivileges=true

[Install]
WantedBy=default.target
"""


def opencode_unit(opencode: str, python: str) -> str:
    return f"""[Unit]
Description=OpenCode Telegram single polling host
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=%h
ExecStart={systemd_quote(opencode)} serve --hostname 127.0.0.1 --port 4097
ExecStartPost={systemd_quote(python)} {systemd_quote(PLUGIN_ROOT / "scripts/warmup.py")}
Restart=on-failure
RestartSec=3
UMask=0077
NoNewPrivileges=true

[Install]
WantedBy=default.target
"""


def systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *args],
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def install_services(mode: str, receiver: str) -> None:
    python = shutil.which("python3") or sys.executable
    codex = shutil.which("codex")
    if not codex:
        raise TelegramBridgeError("codex executable was not found")
    unit_dir = Path.home() / ".config/systemd/user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        unit_dir / "codex-telegram-bridge.service",
        bridge_unit(python, codex),
        0o644,
    )
    atomic_write_text(
        unit_dir / "codex-telegram-poller.service",
        poller_unit(python),
        0o644,
    )
    opencode = shutil.which("opencode")
    if receiver == "opencode":
        if not opencode:
            raise TelegramBridgeError("OpenCode receiver selected but opencode was not found")
        atomic_write_text(
            unit_dir / "opencode-telegram-host.service",
            opencode_unit(opencode, python),
            0o644,
        )

    systemctl("daemon-reload")
    systemctl("enable", "codex-telegram-bridge.service")
    systemctl("restart", "codex-telegram-bridge.service")
    if mode == "standalone":
        systemctl("disable", "--now", "opencode-telegram-host.service", check=False)
        systemctl("enable", "codex-telegram-poller.service")
        systemctl("restart", "codex-telegram-poller.service")
    else:
        systemctl("disable", "--now", "codex-telegram-poller.service", check=False)
        if receiver == "opencode":
            systemctl("enable", "opencode-telegram-host.service")
            systemctl("restart", "opencode-telegram-host.service")


def validate_config(env_path: Path) -> None:
    if os.name != "nt" and stat.S_IMODE(env_path.stat().st_mode) & 0o077:
        raise TelegramBridgeError(
            f"Telegram credential file must not be group/world accessible: {env_path}"
        )
    config = load_telegram_config(env_path)
    if not config.allowed_user_ids:
        raise TelegramBridgeError("TELEGRAM_ALLOWED_USER_IDS must not be empty")
    if config.chat_id not in config.allowed_user_ids:
        raise TelegramBridgeError(
            "private chat ID must be present in TELEGRAM_ALLOWED_USER_IDS"
        )


def test_connection(env_path: Path) -> None:
    config = load_telegram_config(env_path)
    result = _telegram_api(config, "getMe", {})
    if not isinstance(result, dict) or result.get("is_bot") is not True:
        raise TelegramBridgeError("Telegram getMe did not return a bot account")
    print("Telegram connection: PASS")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Configure Codex Telegram shared or standalone mode."
    )
    parser.add_argument("--mode", choices=["shared", "standalone"], required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--reuse-opencode",
        action="store_true",
        help="Reference the existing OpenCode Telegram .env without copying the token.",
    )
    source.add_argument(
        "--env-file",
        type=Path,
        help="Reference an existing Telegram .env file.",
    )
    source.add_argument(
        "--configure-token",
        action="store_true",
        help="Prompt securely and write ~/.config/codex-telegram-plugin/.env.",
    )
    parser.add_argument(
        "--install-services",
        action="store_true",
        help="Generate and start the appropriate user systemd services.",
    )
    parser.add_argument(
        "--test-connection",
        action="store_true",
        help="Call Telegram getMe after configuration.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.reuse_opencode:
        if args.mode != "shared":
            raise TelegramBridgeError("--reuse-opencode requires --mode shared")
        env_path = LEGACY_OPENCODE_ENV_PATH
        shared_dir = LEGACY_OPENCODE_CONFIG_DIR
        receiver = "opencode"
    elif args.configure_token:
        if args.mode != "standalone":
            raise TelegramBridgeError("--configure-token requires --mode standalone")
        env_path = configure_credentials()
        shared_dir = DEFAULT_PLUGIN_CONFIG_DIR
        receiver = "standalone"
    else:
        env_path = args.env_file.expanduser().resolve()
        shared_dir = (
            LEGACY_OPENCODE_CONFIG_DIR
            if args.mode == "shared" and env_path == LEGACY_OPENCODE_ENV_PATH
            else DEFAULT_PLUGIN_CONFIG_DIR
        )
        receiver = "external" if args.mode == "shared" else "standalone"

    if not env_path.is_file():
        raise TelegramBridgeError(f"Telegram env file not found: {env_path}")
    validate_config(env_path)
    install_launchers()
    write_settings(
        mode=args.mode,
        env_path=env_path,
        shared_dir=shared_dir,
        receiver=receiver,
    )
    if args.install_services:
        install_services(args.mode, receiver)
    if args.test_connection:
        test_connection(env_path)
    print(f"mode={args.mode}")
    print(f"receiver={receiver}")
    print(f"settings={DEFAULT_SETTINGS_PATH}")
    print(f"credentials={env_path}")
    print("token_copied=false" if args.reuse_opencode else "token_copied=not-applicable")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except TelegramBridgeError as exc:
        print(f"error: {redact_sensitive_text(exc)}", file=sys.stderr)
        raise SystemExit(2)
