#!/usr/bin/env python3
"""Standalone Telegram polling host for Codex-only installations."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import signal
import socket
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from common import (  # noqa: E402
    TelegramBridgeError,
    TelegramConfig,
    _question_keyboard,
    _question_text,
    _telegram_api,
    atomic_write_json,
    bridge_url,
    cleanup_expired_pending,
    data_dir,
    edit_message,
    load_telegram_config,
    pending_permission_dir,
    pending_question_dir,
    read_json,
    read_pending_json,
    redact_sensitive_text,
    redact_telegram_markup,
    runtime_mode,
    send_message,
)


LOG = logging.getLogger("codex-telegram-poller")
LOCK_STALE_SECONDS = 90
SNAPSHOT_TTL_SECONDS = 3600
CALLBACK_QUESTION_RE = re.compile(r"^q:([^:]+):(\d+):(\d+|c|d)$")
CALLBACK_PERMISSION_RE = re.compile(r"^p:([^:]+):(o|a|r)$")
COMMAND_RE = re.compile(r"^/([a-z_]+)(?:@[A-Za-z0-9_]+)?(?:\s+(.*))?$")


class PollingLock:
    """Uses the same lock name/protocol as the OpenCode Telegram adapter."""

    def __init__(self, config: TelegramConfig) -> None:
        self.path = Path("/tmp") / f"opencoder-telegram-{config.token_hash}.lock"
        self.owned = False

    def _existing_is_live(self) -> bool:
        try:
            metadata = self.path.lstat()
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(metadata.st_mode):
            return True
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            return True
        if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
            return True
        value = read_json(self.path, {})
        if not isinstance(value, dict):
            return self.path.exists() and time.time() - self.path.stat().st_mtime < LOCK_STALE_SECONDS
        try:
            pid = int(value.get("pid", 0))
        except (TypeError, ValueError):
            pid = 0
        hostname = str(value.get("hostname") or "")
        if hostname and hostname != socket.gethostname():
            return time.time() - self.path.stat().st_mtime < LOCK_STALE_SECONDS
        return pid > 0 and Path(f"/proc/{pid}").exists()

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                if self._existing_is_live():
                    return False
                try:
                    self.path.unlink()
                except (FileNotFoundError, PermissionError):
                    return False
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "pid": os.getpid(),
                        "hostname": socket.gethostname(),
                        "createdAt": dt.datetime.now(dt.timezone.utc).isoformat(),
                    },
                    handle,
                )
                handle.write("\n")
            self.owned = True
            return True
        return False

    def refresh(self) -> None:
        if self.owned:
            try:
                self.path.touch()
            except FileNotFoundError:
                self.owned = False

    def release(self) -> None:
        if not self.owned:
            return
        value = read_json(self.path, {})
        if isinstance(value, dict) and value.get("pid") == os.getpid():
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        self.owned = False


class StandaloneTelegramPoller:
    def __init__(self, config: TelegramConfig) -> None:
        self.config = config
        self.stop_requested = False
        self.state_path = data_dir() / "poller-state.json"
        state = read_json(self.state_path, {})
        self.offset = int(state.get("offset", 0)) if isinstance(state, dict) else 0
        cleanup_expired_pending(pending_question_dir(config))
        cleanup_expired_pending(pending_permission_dir(config))

    def api(self, method: str, payload: dict[str, Any], timeout: float = 20) -> Any:
        return _telegram_api(self.config, method, payload, timeout=timeout)

    def _save_offset(self) -> None:
        atomic_write_json(self.state_path, {"offset": self.offset})

    def _is_authorized(self, update: dict[str, Any]) -> bool:
        user = update.get("from")
        message = update.get("message")
        chat = message.get("chat") if isinstance(message, dict) else None
        if not isinstance(user, dict) or not isinstance(chat, dict):
            return False
        user_id = user.get("id")
        chat_id = chat.get("id")
        return (
            chat.get("type") == "private"
            and chat_id == self.config.chat_id
            and user_id in self.config.allowed_user_ids
        )

    def _safe_callback_answer(self, callback_id: Any, text: str | None = None) -> None:
        if not isinstance(callback_id, str):
            return
        payload: dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text[:180]
        try:
            self.api("answerCallbackQuery", payload)
        except TelegramBridgeError as exc:
            LOG.error(
                "answerCallbackQuery failed: %s",
                redact_sensitive_text(exc, extra_secrets=(self.config.bot_token,)),
            )

    def _safe_edit(
        self,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        secrets_to_mask = (self.config.bot_token,)
        payload: dict[str, Any] = {
            "chat_id": self.config.chat_id,
            "message_id": message_id,
            "text": redact_sensitive_text(text, extra_secrets=secrets_to_mask),
        }
        payload["reply_markup"] = redact_telegram_markup(
            reply_markup or {"inline_keyboard": []},
            extra_secrets=secrets_to_mask,
        )
        try:
            self.api("editMessageText", payload)
        except TelegramBridgeError as exc:
            if "message is not modified" not in str(exc):
                LOG.warning("editMessageText failed: %s", exc)

    def _safe_delete(self, message_id: Any) -> None:
        if not isinstance(message_id, int):
            return
        try:
            self.api(
                "deleteMessage",
                {"chat_id": self.config.chat_id, "message_id": message_id},
            )
        except TelegramBridgeError:
            LOG.warning("deleteMessage failed for %s", message_id)

    def _load_question(self, short_hash: str) -> tuple[Path, dict[str, Any]] | None:
        path = pending_question_dir(self.config) / f"{short_hash}.json"
        value = read_pending_json(path)
        return (path, value) if value is not None else None

    def _question_selected(self, pending: dict[str, Any], index: int) -> list[str]:
        answers = pending.get("answersInProgress")
        if not isinstance(answers, list) or index >= len(answers):
            return []
        answer = answers[index]
        return [str(item) for item in answer] if isinstance(answer, list) else []

    def _question_keyboard(
        self,
        short_hash: str,
        question: dict[str, Any],
        index: int,
        selected: list[str],
    ) -> dict[str, Any]:
        keyboard = _question_keyboard(short_hash, question, index)
        if question.get("multiple") is True:
            for row, option in zip(
                keyboard["inline_keyboard"],
                question.get("options") or [],
            ):
                label = str(option.get("label") or "")
                if label in selected:
                    row[0]["text"] = f"✅ {label}"
        return keyboard

    def _save_question(
        self,
        path: Path,
        pending: dict[str, Any],
    ) -> None:
        atomic_write_json(path, pending)

    def _complete_question_if_ready(
        self,
        path: Path,
        short_hash: str,
        pending: dict[str, Any],
        message_id: int,
    ) -> None:
        answers = pending.get("answersInProgress")
        questions = pending.get("questions")
        if not isinstance(answers, list) or not isinstance(questions, list):
            return
        try:
            next_index = answers.index(None)
        except ValueError:
            pending["submittedAt"] = int(time.time() * 1000)
            pending.pop("awaitingCustomFor", None)
            self._save_question(path, pending)
            self._safe_edit(
                message_id,
                self._question_status_text(
                    pending,
                    "⏳ Codex에 답변을 전달하는 중입니다…",
                ),
            )
            return
        pending["currentQuestionIndex"] = next_index
        self._save_question(path, pending)
        question = questions[next_index]
        if not isinstance(question, dict):
            return
        self._safe_edit(
            message_id,
            _question_text(
                questions,
                next_index,
                session_name=str(pending.get("sessionTitle") or "") or None,
            ),
            self._question_keyboard(
                short_hash,
                question,
                next_index,
                self._question_selected(pending, next_index),
            ),
        )

    def _handle_question_callback(
        self,
        match: re.Match[str],
        message_id: int,
        chat_id: int,
        user_id: int,
    ) -> None:
        short_hash, raw_index, selection = match.groups()
        loaded = self._load_question(short_hash)
        if not loaded:
            self._safe_edit(message_id, "이 질문은 이미 처리됐거나 만료됐습니다.")
            return
        path, pending = loaded
        if isinstance(pending.get("submittedAt"), int):
            self._safe_edit(message_id, "이 답변은 이미 Codex로 전달 중입니다.")
            return
        index = int(raw_index)
        questions = pending.get("questions")
        answers = pending.get("answersInProgress")
        if (
            not isinstance(questions, list)
            or not isinstance(answers, list)
            or index >= len(questions)
            or index >= len(answers)
            or not isinstance(questions[index], dict)
        ):
            return
        question = questions[index]

        awaiting = pending.pop("awaitingCustomFor", None)
        if isinstance(awaiting, dict):
            self._safe_delete(awaiting.get("promptMessageId"))

        if selection == "c":
            self._safe_edit(
                message_id,
                self._question_status_text(
                    pending,
                    "✏️ 다음 메시지에 답변을 입력하세요.",
                ),
            )
            prompt_id = send_message(
                self.config,
                "직접 답변을 입력하세요.",
                reply_markup={
                    "force_reply": True,
                    "input_field_placeholder": "답변 입력",
                },
            )
            pending["awaitingCustomFor"] = {
                "shortHash": short_hash,
                "questionIndex": index,
                "chatId": chat_id,
                "userId": user_id,
                "promptMessageId": prompt_id,
            }
            self._save_question(path, pending)
            return

        if selection == "d":
            if question.get("multiple") is not True:
                return
            answers[index] = self._question_selected(pending, index)
            self._complete_question_if_ready(path, short_hash, pending, message_id)
            return

        options = question.get("options")
        option_index = int(selection)
        if (
            not isinstance(options, list)
            or option_index >= len(options)
            or not isinstance(options[option_index], dict)
        ):
            return
        label = str(options[option_index].get("label") or "")
        if question.get("multiple") is True:
            selected = self._question_selected(pending, index)
            answers[index] = (
                [item for item in selected if item != label]
                if label in selected
                else [*selected, label]
            )
            self._save_question(path, pending)
            self._safe_edit(
                message_id,
                _question_text(
                    questions,
                    index,
                    session_name=str(pending.get("sessionTitle") or "") or None,
                ),
                self._question_keyboard(
                    short_hash,
                    question,
                    index,
                    self._question_selected(pending, index),
                ),
            )
            return
        answers[index] = [label]
        self._complete_question_if_ready(path, short_hash, pending, message_id)

    def _handle_custom_answer(
        self,
        text: str,
        chat_id: int,
        user_id: int,
        reply_to_message_id: int,
    ) -> bool:
        root = pending_question_dir(self.config)
        for path in root.glob("*.json") if root.is_dir() else []:
            pending = read_pending_json(path)
            if pending is None or isinstance(pending.get("submittedAt"), int):
                continue
            awaiting = pending.get("awaitingCustomFor")
            if not isinstance(awaiting, dict):
                continue
            if (
                awaiting.get("chatId") != chat_id
                or awaiting.get("userId") != user_id
                or awaiting.get("promptMessageId") != reply_to_message_id
            ):
                continue
            index = int(awaiting.get("questionIndex", -1))
            questions = pending.get("questions")
            answers = pending.get("answersInProgress")
            if (
                not isinstance(questions, list)
                or not isinstance(answers, list)
                or index < 0
                or index >= len(questions)
                or not isinstance(questions[index], dict)
            ):
                return False
            pending.pop("awaitingCustomFor", None)
            if questions[index].get("multiple") is True:
                selected = self._question_selected(pending, index)
                answers[index] = selected if text in selected else [*selected, text]
                self._save_question(path, pending)
                send_message(self.config, "✅ 답변을 추가했습니다. 완료를 누르세요.")
                message_ids = pending.get("telegramMessageIds") or []
                if message_ids and isinstance(message_ids[0], int):
                    self._safe_edit(
                        message_ids[0],
                        _question_text(
                            questions,
                            index,
                            session_name=str(pending.get("sessionTitle") or "") or None,
                        ),
                        self._question_keyboard(
                            path.stem,
                            questions[index],
                            index,
                            self._question_selected(pending, index),
                        ),
                    )
                return True
            answers[index] = [text]
            message_ids = pending.get("telegramMessageIds") or []
            if message_ids and isinstance(message_ids[0], int):
                self._complete_question_if_ready(
                    path,
                    path.stem,
                    pending,
                    message_ids[0],
                )
            return True
        return False

    @staticmethod
    def _question_status_text(pending: dict[str, Any], status: str) -> str:
        session_name = str(pending.get("sessionTitle") or "").strip()
        return f"🧵 세션: {session_name}\n\n{status}" if session_name else status

    def _permission_reply_url(self, pending: dict[str, Any]) -> str:
        base = str(pending.get("serverUrl") or "")
        parsed = urllib.parse.urlsplit(base)
        if parsed.scheme != "http" or parsed.hostname not in {
            "127.0.0.1",
            "::1",
            "localhost",
        }:
            raise TelegramBridgeError("permission reply URL must be localhost HTTP")
        request_id = urllib.parse.quote(str(pending.get("requestID") or ""), safe="")
        return urllib.parse.urljoin(base, f"/permission/{request_id}/reply")

    def _handle_permission_callback(
        self,
        match: re.Match[str],
        message_id: int,
    ) -> None:
        short_hash, selection = match.groups()
        path = pending_permission_dir(self.config) / f"{short_hash}.json"
        pending = read_pending_json(path)
        if pending is None:
            self._safe_edit(message_id, "이 권한 요청은 이미 처리됐거나 만료됐습니다.")
            return
        reply = {"o": "once", "a": "always", "r": "reject"}[selection]
        request = urllib.request.Request(
            self._permission_reply_url(pending),
            data=json.dumps({"reply": reply}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                if response.status != 200:
                    raise TelegramBridgeError(
                        f"permission reply returned HTTP {response.status}"
                    )
        except (urllib.error.URLError, TimeoutError) as exc:
            self._safe_edit(message_id, "⚠️ Codex에 권한 응답을 전달하지 못했습니다.")
            raise TelegramBridgeError("permission reply failed") from exc
        label = {
            "once": "이번 요청을 허용했습니다.",
            "always": "동일 요청을 세션 동안 허용했습니다.",
            "reject": "요청을 거부했습니다.",
        }[reply]
        self._safe_edit(message_id, f"✅ {label}")
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def _bridge_request(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
    ) -> Any:
        base = bridge_url()
        parsed = urllib.parse.urlsplit(base)
        if parsed.scheme != "http" or parsed.hostname not in {
            "127.0.0.1",
            "::1",
            "localhost",
        }:
            raise TelegramBridgeError("Codex bridge must be localhost HTTP")
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            urllib.parse.urljoin(base, path.lstrip("/")),
            data=data,
            headers={"Content-Type": "application/json"} if data else {},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                body = json.load(exc)
            except (json.JSONDecodeError, UnicodeDecodeError):
                body = {"error": str(exc)}
            return exc.code, body

    def _snapshot_path(self) -> Path:
        return data_dir() / "snapshots" / (
            f"{self.config.token_hash}-{self.config.chat_id}.json"
        )

    def _send_sessions(self) -> None:
        _, sessions = self._bridge_request("/session")
        _, statuses = self._bridge_request("/session/status")
        if not isinstance(sessions, list):
            raise TelegramBridgeError("Codex session list is unavailable")
        roots = [
            item
            for item in sessions
            if isinstance(item, dict) and not item.get("parentID")
        ][:20]
        entries = []
        lines = ["📋 Codex 세션"]
        for index, item in enumerate(roots, start=1):
            session_id = str(item.get("id") or "")
            status = (
                statuses.get(session_id, {}).get("type", "idle")
                if isinstance(statuses, dict)
                else "idle"
            )
            title = str(item.get("title") or session_id)[:90]
            entries.append(
                {
                    "index": index,
                    "sessionId": session_id,
                    "title": title,
                    "agent": "codex",
                    "status": status,
                    "serverUrl": bridge_url(),
                }
            )
            icon = {"busy": "🟡", "retry": "🔴"}.get(status, "⚪")
            lines.append(f"{index}. {icon} {title}")
        atomic_write_json(
            self._snapshot_path(),
            {"savedAt": int(time.time()), "entries": entries},
        )
        send_message(
            self.config,
            "\n".join(lines) if entries else "활성 Codex 세션이 없습니다.",
        )

    def _load_snapshot_entry(self, raw_index: str) -> dict[str, Any] | None:
        try:
            index = int(raw_index)
        except ValueError:
            return None
        snapshot = read_json(self._snapshot_path(), {})
        if not isinstance(snapshot, dict):
            return None
        if int(snapshot.get("savedAt", 0)) < int(time.time()) - SNAPSHOT_TTL_SECONDS:
            return None
        entries = snapshot.get("entries")
        if not isinstance(entries, list):
            return None
        return next(
            (
                item
                for item in entries
                if isinstance(item, dict) and item.get("index") == index
            ),
            None,
        )

    def _send_status(self, raw_index: str) -> None:
        entry = self._load_snapshot_entry(raw_index)
        if not entry:
            send_message(self.config, "세션 번호가 없습니다. 먼저 /sessions를 실행하세요.")
            return
        session_id = urllib.parse.quote(str(entry["sessionId"]), safe="")
        status_code, session = self._bridge_request(f"/session/{session_id}")
        if status_code == 404:
            send_message(self.config, "세션이 더 이상 존재하지 않습니다.")
            return
        _, messages = self._bridge_request(f"/session/{session_id}/message?limit=10")
        _, statuses = self._bridge_request("/session/status")
        status = (
            statuses.get(entry["sessionId"], {}).get("type", "idle")
            if isinstance(statuses, dict)
            else "idle"
        )
        user_text = "(메시지 없음)"
        assistant_text = "(메시지 없음)"
        if isinstance(messages, list):
            for envelope in messages:
                if not isinstance(envelope, dict):
                    continue
                role = (envelope.get("info") or {}).get("role")
                parts = envelope.get("parts") or []
                text = " ".join(
                    str(part.get("text") or "")
                    for part in parts
                    if isinstance(part, dict) and part.get("type") == "text"
                ).strip()
                if role == "user" and text:
                    user_text = text[-600:]
                elif role == "assistant" and text:
                    assistant_text = text[-900:]
        title = (
            str(session.get("title") or entry.get("title") or entry["sessionId"])
            if isinstance(session, dict)
            else str(entry.get("title") or entry["sessionId"])
        )
        send_message(
            self.config,
            (
                f"📋 {title}\n"
                f"상태: {status}\n\n"
                f"최근 사용자:\n{user_text}\n\n"
                f"최근 Codex:\n{assistant_text}"
            )[:3900],
        )

    def _start_work(self, raw_index: str) -> None:
        entry = self._load_snapshot_entry(raw_index)
        if not entry:
            send_message(self.config, "세션 번호가 없습니다. 먼저 /sessions를 실행하세요.")
            return
        session_id = urllib.parse.quote(str(entry["sessionId"]), safe="")
        status, payload = self._bridge_request(
            f"/session/{session_id}/command",
            method="POST",
            payload={"command": "start-work"},
        )
        if status != 202:
            reason = payload.get("error", "재개할 수 없습니다") if isinstance(payload, dict) else ""
            send_message(self.config, f"세션 재개 실패: {reason}")
            return
        send_message(self.config, f"✅ {raw_index}번 Codex 세션 재개를 요청했습니다.")

    def _send_help(self) -> None:
        send_message(
            self.config,
            (
                "Codex Telegram 명령\n\n"
                "/sessions - Codex 세션 목록\n"
                "/status N - 세션 상태와 최근 메시지\n"
                "/start_work N - idle 세션 재개\n"
                "/help - 도움말"
            ),
        )

    def _handle_message(self, message: dict[str, Any]) -> None:
        wrapped = {"from": message.get("from"), "message": message}
        if not self._is_authorized(wrapped):
            LOG.warning("ignored unauthorized Telegram message")
            return
        text = message.get("text")
        if not isinstance(text, str):
            return
        chat_id = int(message["chat"]["id"])
        user_id = int(message["from"]["id"])
        reply = message.get("reply_to_message")
        reply_id = reply.get("message_id") if isinstance(reply, dict) else None
        if isinstance(reply_id, int) and self._handle_custom_answer(
            text,
            chat_id,
            user_id,
            reply_id,
        ):
            return
        match = COMMAND_RE.match(text.strip())
        if not match:
            self._send_help()
            return
        command, raw_args = match.groups()
        args = (raw_args or "").split()
        if command == "sessions":
            self._send_sessions()
        elif command == "status":
            self._send_status(args[0] if args else "")
        elif command == "start_work":
            self._start_work(args[0] if args else "")
        elif command == "help":
            self._send_help()

    def _handle_callback(self, callback: dict[str, Any]) -> None:
        message = callback.get("message")
        wrapped = {"from": callback.get("from"), "message": message}
        if not isinstance(message, dict) or not self._is_authorized(wrapped):
            LOG.warning("ignored unauthorized Telegram callback")
            return
        data = callback.get("data")
        message_id = message.get("message_id")
        if not isinstance(data, str) or not isinstance(message_id, int):
            return
        self._safe_callback_answer(callback.get("id"))
        question = CALLBACK_QUESTION_RE.match(data)
        if question:
            self._handle_question_callback(
                question,
                message_id,
                int(message["chat"]["id"]),
                int(callback["from"]["id"]),
            )
            return
        permission = CALLBACK_PERMISSION_RE.match(data)
        if permission:
            self._handle_permission_callback(permission, message_id)

    def handle_update(self, update: dict[str, Any]) -> None:
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            self._handle_callback(callback)
            return
        message = update.get("message")
        if isinstance(message, dict):
            self._handle_message(message)

    def set_commands(self) -> None:
        self.api(
            "setMyCommands",
            {
                "commands": [
                    {"command": "sessions", "description": "Codex 세션 목록"},
                    {"command": "status", "description": "세션 상태 (/status N)"},
                    {"command": "start_work", "description": "세션 재개 (/start_work N)"},
                    {"command": "help", "description": "명령 도움말"},
                ]
            },
        )

    def run(self, lock: PollingLock) -> None:
        self.set_commands()
        LOG.info("standalone Telegram polling started")
        while not self.stop_requested:
            lock.refresh()
            payload: dict[str, Any] = {
                "timeout": 25,
                "allowed_updates": ["message", "callback_query"],
            }
            if self.offset > 0:
                payload["offset"] = self.offset
            try:
                updates = self.api("getUpdates", payload, timeout=35)
            except TelegramBridgeError as exc:
                LOG.error(
                    "getUpdates failed: %s",
                    redact_sensitive_text(exc, extra_secrets=(self.config.bot_token,)),
                )
                time.sleep(3)
                continue
            if not isinstance(updates, list):
                time.sleep(1)
                continue
            for update in updates:
                if not isinstance(update, dict):
                    continue
                try:
                    self.handle_update(update)
                except Exception as exc:
                    LOG.error(
                        "Telegram update handling failed: %s",
                        redact_sensitive_text(exc, extra_secrets=(self.config.bot_token,)),
                    )
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    self.offset = max(self.offset, update_id + 1)
            if updates:
                self._save_offset()


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("CODEX_TELEGRAM_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if runtime_mode() != "standalone":
        raise SystemExit(
            "Standalone poller is disabled because CODEX_TELEGRAM_MODE is not standalone"
        )
    config = load_telegram_config()
    lock = PollingLock(config)
    if not lock.acquire():
        raise SystemExit("Telegram polling lock is owned by another process")
    poller = StandaloneTelegramPoller(config)

    def stop(_signum: int, _frame: Any) -> None:
        poller.stop_requested = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        poller.run(lock)
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
