"""Hermes lifecycle, question, and approval adapter for the shared Telegram bot."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Callable


_IMPORT_LOCK = threading.Lock()


def _bridge_common():
    """Load the Codex checkout selected by setup without copying credentials."""
    installed = Path(__file__).resolve().parent
    pointer = installed / "bridge-source.json"
    root = Path(json.loads(pointer.read_text(encoding="utf-8"))["plugin_root"]) if pointer.exists() else installed.parent
    source = root / "scripts" / "common.py"
    if not source.is_file():
        raise RuntimeError("Codex Telegram checkout is unavailable; rerun scripts/setup.py --refresh")
    name = "_codex_telegram_common_" + hashlib.sha256(str(source).encode()).hexdigest()[:12]
    with _IMPORT_LOCK:
        if name not in sys.modules:
            spec = importlib.util.spec_from_file_location(name, source)
            if spec is None or spec.loader is None:
                raise RuntimeError("Cannot load Codex Telegram helpers")
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            try:
                spec.loader.exec_module(module)
            except Exception:
                sys.modules.pop(name, None)
                raise
        return sys.modules[name]


_CHOICES = {
    "once": "이번만 허용",
    "session": "현재 세션 동안 허용",
    "always": "항상 허용",
    "deny": "거부",
}


class HermesTelegramAdapter:
    def __init__(self, *, send: Callable | None = None, ask: Callable | None = None) -> None:
        self.send = send or self._send
        self.ask = ask or self._ask
        self._lock = threading.Lock()
        self._pending: dict[tuple[str, str], str] = {}
        self._seen: set[tuple[str, str]] = set()

    @staticmethod
    def _send(text: str, title: str) -> None:
        _bridge_common().send_codex_notification(text, title=title)

    @staticmethod
    def _ask(questions: list[dict[str, Any]], **kwargs: Any) -> list[list[str]] | None:
        return _bridge_common().ask_questions(questions, **kwargs)

    @staticmethod
    def _local(platform: str | None) -> bool:
        # Hermes's own Telegram gateway already delivers its answer there.
        return platform != "telegram"

    def on_response(self, *, session_id: str = "", turn_id: str = "", assistant_response: str = "", platform: str = "", **_kwargs: Any) -> None:
        if not self._local(platform) or not turn_id or not isinstance(assistant_response, str):
            return
        with self._lock:
            self._pending[(session_id, turn_id)] = assistant_response
            if len(self._pending) > 500:
                self._pending.pop(next(iter(self._pending)))

    def on_end(self, *, session_id: str = "", turn_id: str = "", completed: bool = False, failed: bool = False, interrupted: bool = False, platform: str = "", **_kwargs: Any) -> None:
        if not turn_id:
            return  # Teardown can emit a second, turnless on_session_end.
        key = (session_id, turn_id)
        with self._lock:
            response = self._pending.pop(key, "")
            if key in self._seen:
                return
            self._seen.add(key)
            if len(self._seen) > 500:
                self._seen.pop()
        if not self._local(platform):
            return
        try:
            common = _bridge_common()
            marker = common.session_emoji(session_id)
            if interrupted:
                self.send("현재 Hermes 작업이 중단되었습니다.", f"{marker} 🚨 Hermes 중단")
            elif completed and not failed and response.strip():
                self.send(response[:3800], f"{marker} ✅ Hermes 완료")
        except Exception:
            # A notification never changes the agent's turn outcome.
            return

    def ask_user(self, args: dict[str, Any], **kwargs: Any) -> str:
        questions = args.get("questions")
        if not isinstance(questions, list) or not 1 <= len(questions) <= 3:
            return json.dumps({"error": "questions must contain 1–3 items"})
        converted = []
        for index, item in enumerate(questions, 1):
            if not isinstance(item, dict) or not isinstance(item.get("question"), str) or not item["question"].strip():
                return json.dumps({"error": "each question needs text"})
            choices = item.get("choices", [])
            if not isinstance(choices, list) or any(not isinstance(choice, str) for choice in choices):
                return json.dumps({"error": "choices must be strings"})
            converted.append({
                "header": str(item.get("header") or f"질문 {index}"),
                "question": item["question"],
                "options": [{"label": choice} for choice in choices],
                "multiple": item.get("multi_select") is True,
                "custom": True,
            })
        session_id = str(kwargs.get("session_id") or kwargs.get("task_id") or uuid.uuid4().hex)
        try:
            answers = self.ask(
                converted,
                session_id=session_id,
                session_name=f"Hermes · {session_id[:8]}",
                server_url=_bridge_common().bridge_url(),
                timeout_seconds=min(max(int(args.get("timeout_seconds", 3600)), 1), 86400),
                request_id=f"hermes-question-{uuid.uuid4().hex}",
                agent_name="Hermes",
            )
            return json.dumps({"answers": answers}, ensure_ascii=False)
        except Exception:
            return json.dumps({"error": "Telegram question unavailable; use Hermes native clarify"})

    def approve(self, request: Any) -> Any:
        options = [{"label": _CHOICES[choice]} for choice in request.allowed_choices if choice in _CHOICES]
        result = "deny"
        try:
            answers = self.ask(
                [{
                    "header": "Hermes 실행 승인",
                    "question": f"{request.description}\n{request.command}",
                    "options": options,
                    "custom": False,
                }],
                session_id=f"hermes-approval-{request.request_id}",
                session_name="Hermes · 실행 승인",
                server_url=_bridge_common().bridge_url(),
                timeout_seconds=max(float(request.timeout_seconds) - 2, 0.1),
                request_id=f"hermes-approval-{request.request_id}",
                agent_name="Hermes",
            )
            selected = answers[0][0] if answers and len(answers) == 1 and len(answers[0]) == 1 else None
            result = next((key for key in request.allowed_choices if _CHOICES.get(key) == selected), "deny")
        except Exception:
            pass  # Host-enforced fail-closed decision; never grant on delivery failure.
        return request.respond(result)

    @staticmethod
    def question_guidance(**_kwargs: Any) -> str:
        return (
            "Telegram 질문을 원하면 hermes_telegram_ask_user 도구를 사용하세요. "
            "전송에 실패하면 Hermes 기본 clarify를 사용하세요. "
            "위험한 명령의 승인은 도구 질문이 아니라 Hermes 승인 절차를 따르세요."
        )


def register(ctx: Any) -> None:
    adapter = HermesTelegramAdapter()
    ctx.register_hook("post_llm_call", adapter.on_response)
    ctx.register_hook("on_session_end", adapter.on_end)
    ctx.register_hook("pre_llm_call", adapter.question_guidance)
    ctx.register_approval_transport("codex-telegram", adapter.approve)
    ctx.register_tool(
        name="hermes_telegram_ask_user",
        toolset="hermes_telegram",
        description="Ask the user through the Codex Telegram plugin bot; do not use for command approvals.",
        schema={
            "name": "hermes_telegram_ask_user",
            "description": "Ask up to three questions via the Codex Telegram plugin bot. For approvals use the native safety gate.",
            "parameters": {
                "type": "object",
                "properties": {
                    "questions": {
                        "type": "array", "minItems": 1, "maxItems": 3,
                        "items": {
                            "type": "object", "required": ["question"],
                            "properties": {
                                "header": {"type": "string"},
                                "question": {"type": "string"},
                                "choices": {"type": "array", "items": {"type": "string"}},
                                "multi_select": {"type": "boolean"},
                            },
                        },
                    },
                    "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 86400},
                },
                "required": ["questions"],
            },
        },
        handler=adapter.ask_user,
    )
