#!/usr/bin/env python3
"""Shared Telegram/OpenCode compatibility helpers.

This module deliberately never calls Telegram getUpdates. The already-running
OpenCode plugin remains the only long-polling consumer for the shared bot token.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import queue
import re
import secrets
import socket
import stat
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable


DEFAULT_PLUGIN_CONFIG_DIR = Path.home() / ".config/codex-telegram-plugin"
DEFAULT_SETTINGS_PATH = DEFAULT_PLUGIN_CONFIG_DIR / "config.json"
DEFAULT_PLUGIN_ENV_PATH = DEFAULT_PLUGIN_CONFIG_DIR / ".env"
LEGACY_OPENCODE_CONFIG_DIR = Path.home() / ".config/opencode/telegram-remote"
LEGACY_OPENCODE_ENV_PATH = LEGACY_OPENCODE_CONFIG_DIR / ".env"
DEFAULT_DATA_DIR = Path.home() / ".local/share/codex-telegram-plugin"
DEFAULT_BRIDGE_HOST = "127.0.0.1"
DEFAULT_BRIDGE_PORT = 43991
RETENTION_MS = 7 * 24 * 60 * 60 * 1000
QUESTION_TIMEOUT_SECONDS = 24 * 60 * 60
PERMISSION_TIMEOUT_SECONDS = 570
TERMINAL_EVENT_LIMIT = 6000
REDACTION_MARKER = "[REDACTED]"
SESSION_EMOJIS = (
    "🐙",
    "🦊",
    "🐼",
    "🐸",
    "🦁",
    "🐯",
    "🐨",
    "🐧",
    "🦄",
    "🐳",
    "🦋",
    "🐝",
    "🦉",
    "🦖",
    "🌵",
    "🍀",
    "🌙",
    "⭐",
    "🔥",
    "💧",
    "🍎",
    "🍋",
    "🍇",
    "🍉",
    "🚀",
    "🎯",
    "🎈",
    "🎲",
    "🧩",
    "🎨",
    "🎵",
    "💎",
)

_SECRET_ENV_KEY_RE = re.compile(
    r"(?:TOKEN|SECRET|PASSWORD|PASSWD|API[_-]?KEY|PRIVATE[_-]?KEY|"
    r"ACCESS[_-]?KEY|AUTHORIZATION)$",
    re.IGNORECASE,
)
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----.*?"
    r"-----END(?: [A-Z0-9]+)? PRIVATE KEY-----",
    re.DOTALL,
)
_TELEGRAM_TOKEN_RE = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b")
_COMMON_TOKEN_RE = re.compile(
    r"\b(?:"
    r"sk-(?:proj-)?[A-Za-z0-9_-]{16,}|"
    r"github_pat_[A-Za-z0-9_]{16,}|"
    r"gh[pousr]_[A-Za-z0-9]{16,}|"
    r"xox[baprs]-[A-Za-z0-9-]{16,}"
    r")\b"
)
_BEARER_TOKEN_RE = re.compile(
    r"(?i)\b(Bearer\s+)([A-Za-z0-9._~+/=-]{12,})"
)
_QUOTED_NAMED_SECRET_RE = re.compile(
    r"(?i)\b("
    r"(?:api[_-]?key|token|secret|password|passwd|authorization|"
    r"access[_-]?key|private[_-]?key)"
    r"\s*[:=]\s*)([\"'])(.*?)(\2)"
)
_UNQUOTED_NAMED_SECRET_RE = re.compile(
    r"(?i)\b("
    r"(?:api[_-]?key|token|secret|password|passwd|authorization|"
    r"access[_-]?key|private[_-]?key)"
    r"\s*[:=]\s*)([^\s,;&#]+)"
)
_SENSITIVE_URL_PARAM_RE = re.compile(
    r"(?i)([?&](?:access_token|api[_-]?key|token|secret|password|"
    r"signature|sig)=)([^&#\s]+)"
)
_NON_SECRET_PLACEHOLDERS = {
    "",
    "bearer",
    "false",
    "none",
    "null",
    "redacted",
    "[redacted]",
    "<redacted>",
    "not-set",
    "unset",
}


class TelegramBridgeError(RuntimeError):
    """Raised when the local Telegram bridge cannot complete a request."""


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str
    chat_id: int
    allowed_user_ids: tuple[int, ...]
    env_path: Path | None

    @property
    def token_hash(self) -> str:
        return hashlib.sha256(self.bot_token.encode("utf-8")).hexdigest()[:16]


def load_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise TelegramBridgeError(f"Telegram env file not found: {path}") from exc
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def settings_path() -> Path:
    return Path(
        os.environ.get("CODEX_TELEGRAM_CONFIG", str(DEFAULT_SETTINGS_PATH))
    ).expanduser()


def load_runtime_settings() -> dict[str, Any]:
    path = settings_path()
    value = read_json(path, {})
    return value if isinstance(value, dict) else {}


def runtime_mode() -> str:
    settings = load_runtime_settings()
    mode = str(
        os.environ.get("CODEX_TELEGRAM_MODE")
        or settings.get("mode")
        or ("shared" if LEGACY_OPENCODE_ENV_PATH.is_file() else "standalone")
    ).strip().lower()
    if mode not in {"shared", "standalone"}:
        raise TelegramBridgeError(
            "CODEX_TELEGRAM_MODE must be 'shared' or 'standalone'"
        )
    return mode


def telegram_env_path() -> Path | None:
    settings = load_runtime_settings()
    configured = os.environ.get("CODEX_TELEGRAM_ENV") or settings.get("env_path")
    if isinstance(configured, str) and configured.strip():
        return Path(configured).expanduser()
    if DEFAULT_PLUGIN_ENV_PATH.is_file():
        return DEFAULT_PLUGIN_ENV_PATH
    if LEGACY_OPENCODE_ENV_PATH.is_file():
        return LEGACY_OPENCODE_ENV_PATH
    return None


def shared_config_dir() -> Path:
    settings = load_runtime_settings()
    configured = (
        os.environ.get("CODEX_TELEGRAM_SHARED_DIR")
        or settings.get("shared_dir")
    )
    if isinstance(configured, str) and configured.strip():
        return Path(configured).expanduser()
    if runtime_mode() == "shared" and LEGACY_OPENCODE_CONFIG_DIR.is_dir():
        return LEGACY_OPENCODE_CONFIG_DIR
    return DEFAULT_PLUGIN_CONFIG_DIR


def terminal_mirror_enabled() -> bool:
    settings = load_runtime_settings()
    configured = os.environ.get("CODEX_TELEGRAM_TERMINAL_MIRROR")
    if configured is None:
        value = settings.get("terminal_mirror", True)
        return value is not False
    return configured.strip().lower() not in {"0", "false", "no", "off"}


def _known_secret_values(extra_secrets: Iterable[str] = ()) -> tuple[str, ...]:
    values: set[str] = set()
    for key, value in os.environ.items():
        if not _SECRET_ENV_KEY_RE.search(key) or len(value) < 6:
            continue
        if value.strip().lower() in _NON_SECRET_PLACEHOLDERS:
            continue
        values.add(value)
    for value in extra_secrets:
        if not isinstance(value, str) or len(value) < 6:
            continue
        if value.strip().lower() in _NON_SECRET_PLACEHOLDERS:
            continue
        values.add(value)
    return tuple(sorted(values, key=len, reverse=True))


def _redact_named_secret(match: re.Match[str]) -> str:
    prefix = match.group(1)
    if match.re is _QUOTED_NAMED_SECRET_RE:
        quote = match.group(2)
        value = match.group(3)
        if value.strip().lower() in _NON_SECRET_PLACEHOLDERS:
            return match.group(0)
        return f"{prefix}{quote}{REDACTION_MARKER}{quote}"
    value = match.group(2)
    if value.strip().lower() in _NON_SECRET_PLACEHOLDERS:
        return match.group(0)
    return f"{prefix}{REDACTION_MARKER}"


def redact_sensitive_text(
    value: Any,
    *,
    extra_secrets: Iterable[str] = (),
) -> str:
    """Mask credentials before text leaves the local process."""

    text = str(value)
    for secret in _known_secret_values(extra_secrets):
        text = text.replace(secret, REDACTION_MARKER)
        escaped = html.escape(secret, quote=False)
        if escaped != secret:
            text = text.replace(escaped, REDACTION_MARKER)
    text = _PRIVATE_KEY_RE.sub(REDACTION_MARKER, text)
    text = _TELEGRAM_TOKEN_RE.sub(REDACTION_MARKER, text)
    text = _COMMON_TOKEN_RE.sub(REDACTION_MARKER, text)
    text = _BEARER_TOKEN_RE.sub(rf"\1{REDACTION_MARKER}", text)
    text = _QUOTED_NAMED_SECRET_RE.sub(_redact_named_secret, text)
    text = _UNQUOTED_NAMED_SECRET_RE.sub(_redact_named_secret, text)
    text = _SENSITIVE_URL_PARAM_RE.sub(rf"\1{REDACTION_MARKER}", text)

    home_paths = {str(Path.home()), os.environ.get("HOME", "")}
    for home_path in sorted(home_paths, key=len, reverse=True):
        if home_path and home_path != "/":
            text = text.replace(home_path, "~")
    hostname = socket.gethostname().strip()
    if len(hostname) >= 3:
        text = re.sub(
            rf"(?<![A-Za-z0-9_.-]){re.escape(hostname)}(?![A-Za-z0-9_.-])",
            "[LOCAL_HOST]",
            text,
            flags=re.IGNORECASE,
        )
    return text


def redact_telegram_markup(
    value: Any,
    *,
    extra_secrets: Iterable[str] = (),
) -> Any:
    """Mask user-visible strings in Telegram reply markup without changing actions."""

    if isinstance(value, list):
        return [
            redact_telegram_markup(item, extra_secrets=extra_secrets)
            for item in value
        ]
    if not isinstance(value, dict):
        return value
    redacted: dict[str, Any] = {}
    for key, item in value.items():
        if key in {"text", "input_field_placeholder"} and isinstance(item, str):
            redacted[key] = redact_sensitive_text(
                item,
                extra_secrets=extra_secrets,
            )
        else:
            redacted[key] = redact_telegram_markup(
                item,
                extra_secrets=extra_secrets,
            )
    return redacted


def _terminal_safe(value: Any) -> str:
    text = redact_sensitive_text(value)
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "?", text)
    return text[:TERMINAL_EVENT_LIMIT]


def mirror_terminal_event(
    kind: str,
    request_id: str,
    title: str,
    details: Iterable[str] = (),
    *,
    state: str,
    stream: Any | None = None,
) -> bool:
    """Mirror a Telegram request or result to an interactive Codex terminal."""

    if not terminal_mirror_enabled():
        return False
    lines = [
        "",
        (
            f"┌─ Codex Telegram · {_terminal_safe(kind)} · "
            f"{_terminal_safe(request_id)}"
        ),
        f"│ {_terminal_safe(title)}",
    ]
    for detail in details:
        for line in _terminal_safe(detail).splitlines() or [""]:
            lines.append(f"│ {line}")
    lines.extend([f"│ 상태: {_terminal_safe(state)}", "└─", ""])
    payload = "\n".join(lines)

    if stream is not None:
        stream.write(payload)
        stream.flush()
        return True
    if sys.stderr.isatty():
        sys.stderr.write(payload)
        sys.stderr.flush()
        return True
    flags = os.O_WRONLY | os.O_NONBLOCK
    if hasattr(os, "O_NOCTTY"):
        flags |= os.O_NOCTTY
    try:
        fd = os.open("/dev/tty", flags)
    except OSError:
        return False
    try:
        os.write(fd, payload.encode("utf-8", errors="replace"))
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def load_telegram_config(env_path: Path | None = None) -> TelegramConfig:
    path = env_path.expanduser() if env_path is not None else telegram_env_path()
    if path is not None and path.is_file() and os.name != "nt":
        metadata = path.stat()
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise TelegramBridgeError(
                "Telegram credential file must not be group/world accessible: "
                f"{redact_sensitive_text(path)}"
            )
        if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
            raise TelegramBridgeError(
                "Telegram credential file must be owned by the current user: "
                f"{redact_sensitive_text(path)}"
            )
    values = load_dotenv(path) if path is not None and path.is_file() else {}
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or values.get("TELEGRAM_BOT_TOKEN")
    raw_chat_id = os.environ.get("TELEGRAM_CHAT_ID") or values.get("TELEGRAM_CHAT_ID")
    raw_allowed = (
        os.environ.get("TELEGRAM_ALLOWED_USER_IDS")
        or values.get("TELEGRAM_ALLOWED_USER_IDS")
        or ""
    )
    if not token:
        raise TelegramBridgeError("TELEGRAM_BOT_TOKEN is not configured")
    if not raw_chat_id:
        raise TelegramBridgeError("TELEGRAM_CHAT_ID is not configured")
    try:
        chat_id = int(raw_chat_id)
        allowed = tuple(int(item.strip()) for item in raw_allowed.split(",") if item.strip())
    except ValueError as exc:
        raise TelegramBridgeError("Telegram chat/user IDs must be integers") from exc
    if not allowed:
        raise TelegramBridgeError("TELEGRAM_ALLOWED_USER_IDS must not be empty")
    if chat_id not in allowed:
        # Private chats normally use the user id as chat id. This prevents
        # accidentally targeting a different room than the OpenCode setup.
        raise TelegramBridgeError(
            "TELEGRAM_CHAT_ID is not present in TELEGRAM_ALLOWED_USER_IDS"
        )
    return TelegramConfig(token, chat_id, allowed, path)


def data_dir() -> Path:
    path = Path(
        os.environ.get("CODEX_TELEGRAM_DATA_DIR")
        or os.environ.get("PLUGIN_DATA")
        or DEFAULT_DATA_DIR
    ).expanduser()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def atomic_write_json(path: Path, payload: Any, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def read_pending_json(path: Path) -> dict[str, Any] | None:
    value = read_json(path)
    if not isinstance(value, dict):
        try:
            path.unlink()
        except OSError:
            pass
        return None
    expires_at = value.get("expiresAt")
    if type(expires_at) is not int or expires_at <= int(time.time() * 1000):
        try:
            path.unlink()
        except OSError:
            pass
        return None
    return value


def cleanup_expired_pending(root: Path) -> None:
    if not root.is_dir():
        return
    for path in root.glob("*.json"):
        read_pending_json(path)


def _telegram_api(
    config: TelegramConfig,
    method: str,
    payload: dict[str, Any],
    timeout: float = 20.0,
) -> Any:
    body = urllib.parse.urlencode(
        {
            key: json.dumps(value, ensure_ascii=False)
            if isinstance(value, (dict, list))
            else str(value)
            for key, value in payload.items()
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{config.bot_token}/{method}",
        data=body,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            decoded = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        safe_error = redact_sensitive_text(exc, extra_secrets=(config.bot_token,))
        raise TelegramBridgeError(f"Telegram {method} failed: {safe_error}") from exc
    if not decoded.get("ok"):
        description = redact_sensitive_text(
            decoded.get("description", "unknown"),
            extra_secrets=(config.bot_token,),
        )
        raise TelegramBridgeError(
            f"Telegram {method} rejected the request: {description}"
        )
    return decoded.get("result")


def send_message(
    config: TelegramConfig,
    text: str,
    *,
    reply_markup: dict[str, Any] | None = None,
    parse_mode: str | None = None,
    silent: bool = False,
) -> int:
    secrets_to_mask = (config.bot_token,)
    payload: dict[str, Any] = {
        "chat_id": config.chat_id,
        "text": redact_sensitive_text(text, extra_secrets=secrets_to_mask),
    }
    if reply_markup is not None:
        payload["reply_markup"] = redact_telegram_markup(
            reply_markup,
            extra_secrets=secrets_to_mask,
        )
    if parse_mode is not None:
        payload["parse_mode"] = parse_mode
    if silent:
        payload["disable_notification"] = True
    result = _telegram_api(config, "sendMessage", payload)
    if not isinstance(result, dict):
        raise TelegramBridgeError("Telegram sendMessage response is not an object")
    message_id = result.get("message_id")
    if not isinstance(message_id, int):
        raise TelegramBridgeError("Telegram sendMessage response has no message_id")
    return message_id


def edit_message(
    config: TelegramConfig,
    message_id: int,
    text: str,
    *,
    remove_keyboard: bool = True,
) -> None:
    payload: dict[str, Any] = {
        "chat_id": config.chat_id,
        "message_id": message_id,
        "text": redact_sensitive_text(
            text,
            extra_secrets=(config.bot_token,),
        ),
    }
    if remove_keyboard:
        payload["reply_markup"] = {"inline_keyboard": []}
    _telegram_api(config, "editMessageText", payload)


def send_codex_notification(
    text: str,
    *,
    title: str = "Codex",
    config: TelegramConfig | None = None,
    silent: bool = False,
) -> int:
    cfg = config or load_telegram_config()
    clipped = text.strip()
    if len(clipped) > 3500:
        clipped = clipped[:3499] + "…"
    safe_title = html.escape(title, quote=False)
    safe_body = html.escape(clipped or "(내용 없음)", quote=False)
    return send_message(
        cfg,
        f"<b>{safe_title}</b>\n\n{safe_body}",
        parse_mode="HTML",
        silent=silent,
    )


def _short_hash(source: str) -> str:
    digest = hashlib.sha256(source.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")[:10]


def question_short_hash(request_id: str, session_id: str, server_url: str) -> str:
    return _short_hash(f"{server_url}:{session_id}:{request_id}")


def permission_short_hash(
    request_id: str,
    session_id: str,
    endpoint: str,
    server_url: str,
) -> str:
    return _short_hash(f"{server_url}:{endpoint}:{session_id}:{request_id}")


def pending_question_dir(config: TelegramConfig) -> Path:
    return Path(tempfile.gettempdir()) / (
        f"opencoder-telegram-pending-questions-{config.token_hash}"
    )


def pending_permission_dir(config: TelegramConfig) -> Path:
    return Path(tempfile.gettempdir()) / (
        f"opencoder-telegram-pending-permissions-{config.token_hash}"
    )


def _question_text(
    questions: list[dict[str, Any]],
    index: int,
    *,
    session_name: str | None = None,
) -> str:
    question = questions[index]
    progress = f"질문 {index + 1}/{len(questions)} · " if len(questions) > 1 else ""
    header = str(question.get("header") or "질문")
    prompt = str(question.get("question") or "")
    lines: list[str] = []
    if session_name:
        lines.extend([f"🧵 세션: {session_name}", ""])
    lines.extend([f"❓ [Codex] {progress}{header}", "", prompt])
    options = question.get("options") or []
    if options:
        lines.extend(["", "Options:", ""])
        for option_index, option in enumerate(options, start=1):
            label = str(option.get("label") or f"Option {option_index}")
            description = str(option.get("description") or "").strip()
            lines.append(f"{option_index}. {label}")
            if description:
                lines.append(f"설명: {description}")
            if option_index != len(options):
                lines.append("")
    return "\n".join(lines)


def _question_keyboard(
    short_hash: str,
    question: dict[str, Any],
    question_index: int,
) -> dict[str, Any]:
    rows = [
        [
            {
                "text": str(option.get("label") or f"Option {index + 1}"),
                "callback_data": f"q:{short_hash}:{question_index}:{index}",
            }
        ]
        for index, option in enumerate(question.get("options") or [])
    ]
    if question.get("custom", True) is not False:
        rows.append(
            [
                {
                    "text": "✏️ Custom answer",
                    "callback_data": f"q:{short_hash}:{question_index}:c",
                }
            ]
        )
    if question.get("multiple") is True:
        rows.append(
            [
                {
                    "text": "✅ Done",
                    "callback_data": f"q:{short_hash}:{question_index}:d",
                }
            ]
        )
    return {"inline_keyboard": rows}


def normalize_questions(raw_questions: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_questions):
        if not isinstance(raw, dict):
            raise TelegramBridgeError("Each question must be an object")
        prompt = raw.get("question")
        if not isinstance(prompt, str) or not prompt.strip():
            raise TelegramBridgeError("Each question needs non-empty question text")
        header = raw.get("header")
        options_raw = raw.get("options") or []
        if not isinstance(options_raw, list):
            raise TelegramBridgeError("Question options must be an array")
        options: list[dict[str, str]] = []
        for option_index, option in enumerate(options_raw):
            if isinstance(option, str):
                options.append({"label": option, "description": ""})
                continue
            if not isinstance(option, dict) or not isinstance(option.get("label"), str):
                raise TelegramBridgeError(
                    f"Question {index + 1} option {option_index + 1} needs a label"
                )
            options.append(
                {
                    "label": option["label"],
                    "description": str(option.get("description") or ""),
                }
            )
        question: dict[str, Any] = {
            "header": str(header or f"질문 {index + 1}"),
            "question": prompt,
            "options": options,
            "multiple": raw.get("multiple") is True,
            "custom": raw.get("custom", True) is not False,
        }
        if not options and question["custom"] is False:
            raise TelegramBridgeError("A question needs options or custom input")
        questions.append(question)
    if not questions:
        raise TelegramBridgeError("At least one question is required")
    return questions


def ask_questions(
    questions: Iterable[dict[str, Any]],
    *,
    session_id: str,
    server_url: str,
    timeout_seconds: float = QUESTION_TIMEOUT_SECONDS,
    config: TelegramConfig | None = None,
    request_id: str | None = None,
    session_name: str | None = None,
    cancel_event: threading.Event | None = None,
    cancel_message: str = "✅ Codex에서 답변을 받았습니다.",
) -> list[list[str]] | None:
    cfg = config or load_telegram_config()
    normalized = normalize_questions(questions)
    if cancel_event is not None and cancel_event.is_set():
        return None
    request_id = request_id or f"codex-question-{secrets.token_urlsafe(12)}"
    owner_id = f"codex-{os.getpid()}-{secrets.token_hex(6)}"
    short_hash = question_short_hash(request_id, session_id, server_url)
    display_session_name = session_name or session_title(os.getcwd(), session_id)
    now_ms = int(time.time() * 1000)
    message_id = send_message(
        cfg,
        _question_text(normalized, 0, session_name=display_session_name),
        reply_markup=_question_keyboard(short_hash, normalized[0], 0),
    )
    path = pending_question_dir(cfg) / f"{short_hash}.json"
    payload = {
        "requestID": request_id,
        "sessionID": session_id,
        "sessionTitle": display_session_name,
        "serverUrl": server_url,
        "ownerInstanceID": owner_id,
        "ownerPID": os.getpid(),
        "questions": normalized,
        "sentAt": now_ms,
        "expiresAt": now_ms + RETENTION_MS,
        "telegramMessageIds": [message_id],
        "currentQuestionIndex": 0,
        "answersInProgress": [None for _ in normalized],
    }
    atomic_write_json(path, payload)
    terminal_details: list[str] = []
    for index, question in enumerate(normalized, start=1):
        terminal_details.append(
            f"{index}. {question['header']}: {question['question']}"
        )
        labels = [
            str(option.get("label") or "")
            for option in question.get("options") or []
        ]
        if labels:
            terminal_details.append(f"   선택지: {', '.join(labels)}")
    mirror_terminal_event(
        "질문",
        short_hash,
        "사용자 답변을 기다립니다.",
        terminal_details,
        state="Telegram과 현재 터미널에 표시됨 · Telegram 답변 대기",
    )
    deadline = time.monotonic() + timeout_seconds
    try:
        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                try:
                    edit_message(
                        cfg,
                        message_id,
                        f"🧵 세션: {display_session_name}\n\n{cancel_message}",
                    )
                except TelegramBridgeError:
                    pass
                mirror_terminal_event(
                    "질문",
                    short_hash,
                    "다른 채널의 답변이 먼저 도착했습니다.",
                    state="다른 채널 응답 완료",
                )
                return None
            current = read_pending_json(path)
            if isinstance(current, dict) and isinstance(current.get("submittedAt"), int):
                answers = current.get("answersInProgress")
                if isinstance(answers, list):
                    normalized_answers = [
                        [str(value) for value in (answer or [])] for answer in answers
                    ]
                    try:
                        edit_message(
                            cfg,
                            message_id,
                            (
                                f"🧵 세션: {display_session_name}\n\n"
                                "✅ Codex에 답변을 전달했습니다."
                            ),
                        )
                    except TelegramBridgeError:
                        pass
                    answer_details = []
                    for index, answer in enumerate(normalized_answers, start=1):
                        answer_details.append(
                            f"{index}. {', '.join(answer) if answer else '(빈 답변)'}"
                        )
                    mirror_terminal_event(
                        "질문",
                        short_hash,
                        "Telegram 답변을 Codex에 전달했습니다.",
                        answer_details,
                        state="응답 완료",
                    )
                    return normalized_answers
            time.sleep(0.35)
        try:
            edit_message(
                cfg,
                message_id,
                f"🧵 세션: {display_session_name}\n\n⌛ Codex 질문이 만료됐습니다.",
            )
        except TelegramBridgeError:
            pass
        mirror_terminal_event(
            "질문",
            short_hash,
            "사용자 답변을 받지 못했습니다.",
            state="만료",
        )
        raise TimeoutError("Telegram question timed out")
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


class _PermissionReplyHandler(BaseHTTPRequestHandler):
    server_version = "CodexTelegramPermission/1.0"

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        server = self.server
        assert isinstance(server, _PermissionHTTPServer)
        expected = f"/permission/{urllib.parse.quote(server.request_id, safe='')}/reply"
        if urllib.parse.urlsplit(self.path).path != expected:
            self.send_error(404)
            return
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 65536)
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            reply = body.get("reply")
            if reply not in {"once", "always", "reject"}:
                raise ValueError("invalid reply")
            server.replies.put_nowait(reply)
        except (ValueError, json.JSONDecodeError, queue.Full):
            self.send_error(400)
            return
        encoded = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


class _PermissionHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, request_id: str) -> None:
        super().__init__((DEFAULT_BRIDGE_HOST, 0), _PermissionReplyHandler)
        self.request_id = request_id
        self.replies: queue.Queue[str] = queue.Queue(maxsize=1)


def request_permission(
    *,
    session_id: str,
    title: str,
    permission: str,
    patterns: list[str],
    directory: str,
    timeout_seconds: float = PERMISSION_TIMEOUT_SECONDS,
    config: TelegramConfig | None = None,
) -> str:
    cfg = config or load_telegram_config()
    request_id = f"codex-permission-{secrets.token_urlsafe(12)}"
    server = _PermissionHTTPServer(request_id)
    host, port = server.server_address
    server_url = f"http://{host}:{port}/"
    short_hash = permission_short_hash(request_id, session_id, "request", server_url)
    detail_lines = [
        "❓ [Codex] 권한 요청",
        "",
        f"세션: {session_id}",
        f"권한: {permission}",
        f"내용: {title}",
    ]
    if patterns:
        detail_lines.append(f"대상: {', '.join(patterns)}")
    keyboard = {
        "inline_keyboard": [
            [{"text": "✅ 이번만 허용", "callback_data": f"p:{short_hash}:o"}],
            [{"text": "♻️ 동일 요청 항상 허용", "callback_data": f"p:{short_hash}:a"}],
            [{"text": "❌ 거부", "callback_data": f"p:{short_hash}:r"}],
        ]
    }
    message_id = send_message(cfg, "\n".join(detail_lines), reply_markup=keyboard)
    now_ms = int(time.time() * 1000)
    path = pending_permission_dir(cfg) / f"{short_hash}.json"
    atomic_write_json(
        path,
        {
            "requestID": request_id,
            "sessionID": session_id,
            "serverUrl": server_url,
            "directory": directory,
            "title": title,
            "permission": permission,
            "patterns": patterns,
            "always": patterns,
            "sentAt": now_ms,
            "expiresAt": now_ms + RETENTION_MS,
            "telegramMessageId": message_id,
            "endpoint": "request",
        },
    )
    terminal_details = [
        f"세션: {session_id}",
        f"권한: {permission}",
        f"내용: {title}",
    ]
    if patterns:
        terminal_details.append(f"대상: {', '.join(patterns)}")
    terminal_details.append("선택: 이번만 허용 / 동일 요청 항상 허용 / 거부")
    mirror_terminal_event(
        "권한 요청",
        short_hash,
        "Codex 실행 승인을 기다립니다.",
        terminal_details,
        state="Telegram과 현재 터미널에 표시됨 · Telegram 결정 대기",
    )
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        reply = server.replies.get(timeout=timeout_seconds)
        label = {
            "once": "이번 요청 허용",
            "always": "동일 요청을 현재 세션 동안 허용",
            "reject": "요청 거부",
        }[reply]
        mirror_terminal_event(
            "권한 요청",
            short_hash,
            "Telegram 권한 결정을 Codex에 전달했습니다.",
            [f"결정: {label}"],
            state="응답 완료",
        )
        return reply
    except queue.Empty as exc:
        try:
            edit_message(cfg, message_id, "⌛ Codex 권한 요청이 만료됐습니다.")
        except TelegramBridgeError:
            pass
        mirror_terminal_event(
            "권한 요청",
            short_hash,
            "권한 결정을 받지 못했습니다.",
            state="만료 · Codex 네이티브 승인 흐름으로 복귀",
        )
        raise TimeoutError("Telegram permission timed out") from exc
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def bridge_url() -> str:
    host = os.environ.get("CODEX_TELEGRAM_BRIDGE_HOST", DEFAULT_BRIDGE_HOST)
    port = int(os.environ.get("CODEX_TELEGRAM_BRIDGE_PORT", str(DEFAULT_BRIDGE_PORT)))
    return f"http://{host}:{port}/"


def resolve_session_name(
    session_id: str,
    *,
    fallback: str,
    server_url: str | None = None,
) -> str:
    """Resolve the current Codex thread title from the localhost bridge."""

    normalized_fallback = " ".join(str(fallback).split())[:100] or "Codex"
    if not session_id:
        return normalized_fallback
    base = server_url or bridge_url()
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme != "http" or parsed.hostname not in {
        "127.0.0.1",
        "::1",
        "localhost",
    }:
        return normalized_fallback
    url = urllib.parse.urljoin(
        base,
        f"session/{urllib.parse.quote(session_id, safe='')}",
    )
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            session = json.load(response)
    except (OSError, TimeoutError, json.JSONDecodeError):
        return normalized_fallback
    if not isinstance(session, dict):
        return normalized_fallback
    title = " ".join(str(session.get("title") or "").split())
    return title[:100] or normalized_fallback


def _registry_filename(session_id: str) -> str:
    encoded = base64.urlsafe_b64encode(session_id.encode("utf-8")).decode("ascii")
    return encoded.rstrip("=") + ".json"


def session_registry_dir(config: TelegramConfig) -> Path:
    return shared_config_dir() / "session-registry" / config.token_hash


def register_session(
    session_id: str,
    *,
    title: str,
    cwd: str,
    status: str = "idle",
    config: TelegramConfig | None = None,
) -> None:
    del cwd  # Remote status lookup provides the authoritative cwd.
    cfg = config or load_telegram_config()
    normalized_status = status if status in {"idle", "busy", "retry"} else "idle"
    path = session_registry_dir(cfg) / _registry_filename(session_id)
    atomic_write_json(
        path,
        {
            "version": 1,
            "entry": {
                "sessionId": session_id,
                "title": title,
                "parentID": None,
                "agent": "codex",
                "status": normalized_status,
                "serverUrl": bridge_url(),
                "updatedAt": int(time.time() * 1000),
            },
        },
    )


def session_title(cwd: str, session_id: str) -> str:
    name = Path(cwd).name or cwd or "Codex"
    return f"Codex · {name} · {session_id[:8]}"


def session_emoji(session_id: str) -> str:
    """Return a random-looking marker that stays stable for one session."""

    normalized = str(session_id).strip()
    if not normalized:
        return "💬"
    digest = hashlib.sha256(normalized.encode("utf-8")).digest()
    index = int.from_bytes(digest[:2], "big") % len(SESSION_EMOJIS)
    return SESSION_EMOJIS[index]


def permission_fingerprint(tool_name: str, tool_input: Any) -> str:
    encoded = json.dumps(
        {"tool_name": tool_name, "tool_input": tool_input},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _always_path() -> Path:
    return data_dir() / "always-permissions.json"


def is_always_allowed(session_id: str, fingerprint: str) -> bool:
    data = read_json(_always_path(), {})
    if not isinstance(data, dict):
        return False
    values = data.get(session_id)
    return isinstance(values, list) and fingerprint in values


def remember_always_allowed(session_id: str, fingerprint: str) -> None:
    path = _always_path()
    data = read_json(path, {})
    if not isinstance(data, dict):
        data = {}
    values = data.get(session_id)
    if not isinstance(values, list):
        values = []
    if fingerprint not in values:
        values.append(fingerprint)
    data[session_id] = values[-200:]
    atomic_write_json(path, data)


def clear_session_permissions(session_id: str) -> None:
    path = _always_path()
    data = read_json(path, {})
    if not isinstance(data, dict) or session_id not in data:
        return
    data.pop(session_id, None)
    atomic_write_json(path, data)


def summarize_tool_input(tool_input: Any, limit: int = 1200) -> tuple[str, list[str]]:
    if isinstance(tool_input, dict):
        description = tool_input.get("description")
        command = tool_input.get("command")
        title = (
            str(description)
            if isinstance(description, str) and description.strip()
            else str(command)
            if isinstance(command, str) and command.strip()
            else json.dumps(tool_input, ensure_ascii=False, sort_keys=True)
        )
        patterns = []
        if isinstance(command, str) and command.strip():
            patterns.append(command.strip()[:500])
    else:
        title = str(tool_input)
        patterns = []
    title = re.sub(r"\s+", " ", title).strip()
    return (title[:limit] or "(세부 정보 없음)", patterns)
