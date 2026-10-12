#!/usr/bin/env python3
"""Configure shared or standalone Codex Telegram installations."""

from __future__ import annotations

import argparse
import getpass
import hashlib
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
HERMES_PLUGIN_NAME = "codex-telegram-bridge"
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
    load_runtime_settings,
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


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")).expanduser().resolve()


def _hermes_command(hermes: str, *args: str) -> str:
    try:
        completed = subprocess.run(
            [hermes, *args], check=True, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise TelegramBridgeError("Hermes plugin configuration failed; inspect Hermes locally") from exc
    return completed.stdout.strip()


def install_hermes() -> bool:
    """Refresh the opt-in Hermes adapter in the active profile, if Hermes is installed."""
    hermes = shutil.which("hermes")
    if not hermes:
        return False
    destination = hermes_home() / "plugins" / HERMES_PLUGIN_NAME
    marker = destination / "bridge-source.json"
    source = PLUGIN_ROOT / "hermes_plugin"
    filenames = ("plugin.yaml", "__init__.py")
    contents = {name: (source / name).read_text(encoding="utf-8") for name in filenames}
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or not marker.is_file():
            raise TelegramBridgeError("Hermes plugin directory is not managed by this installer")
        try:
            existing = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TelegramBridgeError("Hermes plugin ownership marker is invalid") from exc
        if existing.get("managed_by") != "codex-telegram-plugin":
            raise TelegramBridgeError("Hermes plugin directory is not managed by this installer")
        installed_hashes = existing.get("installed_sha256") or {
            name: hashlib.sha256(contents[name].encode("utf-8")).hexdigest() for name in filenames
        }
        for name in filenames:
            target = destination / name
            if (
                target.is_symlink()
                or not target.is_file()
                or hashlib.sha256(target.read_bytes()).hexdigest() != installed_hashes.get(name)
            ):
                raise TelegramBridgeError("Hermes adapter has local changes; refusing to overwrite")
    for filename, text in contents.items():
        atomic_write_text(destination / filename, text, 0o644)
    atomic_write_text(
        marker,
        json.dumps({
            "managed_by": "codex-telegram-plugin", "plugin_root": str(PLUGIN_ROOT),
            "installed_sha256": {
                name: hashlib.sha256(text.encode("utf-8")).hexdigest()
                for name, text in contents.items()
            },
        }, indent=2) + "\n",
        0o600,
    )
    _hermes_command(hermes, "plugins", "enable", HERMES_PLUGIN_NAME)
    selected = _hermes_command(hermes, "config", "get", "security.approval.transport")
    if selected in {"", "builtin", "codex-telegram"}:
        if selected != "codex-telegram":
            _hermes_command(hermes, "config", "set", "security.approval.transport", "codex-telegram")
    else:
        print("Hermes approval transport is already customized; leaving it unchanged")
    return True


def write_settings(
    *,
    mode: str,
    env_path: Path,
    shared_dir: Path,
    receiver: str,
) -> None:
    previous = load_runtime_settings()
    payload = dict(previous)
    payload.update({
        "version": 1,
        "mode": mode,
        "env_path": str(env_path),
        "shared_dir": str(shared_dir),
        "receiver": receiver,
        "terminal_mirror": previous.get("terminal_mirror") is not False,
    })
    if previous.get("question_routing") == "telegram_choices":
        payload["question_routing"] = "telegram_choices"
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
    parser.add_argument("--mode", choices=["shared", "standalone"])
    source = parser.add_mutually_exclusive_group()
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
    parser.add_argument(
        "--refresh", action="store_true",
        help="Reapply the existing receiver configuration and refresh Hermes integration.",
    )
    args = parser.parse_args()
    if args.refresh:
        if args.mode or args.reuse_opencode or args.env_file or args.configure_token:
            parser.error("--refresh cannot be combined with --mode or a credential source")
    elif not args.mode or not (args.reuse_opencode or args.env_file or args.configure_token):
        parser.error("initial setup requires --mode and a credential source")
    return args


def main() -> int:
    args = parse_args()
    if args.refresh:
        settings = load_runtime_settings()
        if (
            settings.get("mode") not in {"shared", "standalone"}
            or not settings.get("env_path")
            or not settings.get("shared_dir")
            or settings.get("receiver") not in {"opencode", "external", "standalone"}
        ):
            raise TelegramBridgeError("No existing Codex Telegram configuration to refresh")
        mode = settings["mode"]
        env_path = Path(settings["env_path"])
        shared_dir = Path(settings["shared_dir"])
        receiver = settings["receiver"]
    elif args.reuse_opencode:
        mode = args.mode
        if args.mode != "shared":
            raise TelegramBridgeError("--reuse-opencode requires --mode shared")
        env_path = LEGACY_OPENCODE_ENV_PATH
        shared_dir = LEGACY_OPENCODE_CONFIG_DIR
        receiver = "opencode"
    elif args.configure_token:
        mode = args.mode
        if args.mode != "standalone":
            raise TelegramBridgeError("--configure-token requires --mode standalone")
        env_path = configure_credentials()
        shared_dir = DEFAULT_PLUGIN_CONFIG_DIR
        receiver = "standalone"
    else:
        mode = args.mode
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
        mode=mode,
        env_path=env_path,
        shared_dir=shared_dir,
        receiver=receiver,
    )
    hermes_enabled = install_hermes()
    if args.install_services:
        install_services(mode, receiver)
    if args.test_connection:
        test_connection(env_path)
    print(f"mode={mode}")
    print(f"receiver={receiver}")
    print(f"hermes_adapter={'enabled' if hermes_enabled else 'not-installed'}")
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
