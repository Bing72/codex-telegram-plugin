"""Hermes lifecycle, question, and approval adapter for the shared Telegram bot."""

from __future__ import annotations

import contextvars
import hashlib
import importlib.util
import json
import logging
import queue
import sqlite3
import sys
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any, Callable


_IMPORT_LOCK = threading.Lock()
_LOG = logging.getLogger(__name__)


def _bridge_common():
    """Load the Codex checkout selected by setup without copying credentials."""
    installed = Path(__file__).resolve().parent
    pointer = installed / "bridge-source.json"
    root = Path(json.loads(pointer.read_text(encoding="utf-8"))["plugin_root"]) if pointer.exists() else installed.parent
    source = root / "scripts" / "common.py"
    if not source.is_file():
        raise RuntimeError("Codex Telegram checkout is unavailable; rerun scripts/setup.py --refresh")
    source_bytes = source.read_bytes()
    name = "_codex_telegram_common_" + hashlib.sha256(str(source).encode() + b"\0" + source_bytes).hexdigest()[:12]
    with _IMPORT_LOCK:
        if name not in sys.modules:
            spec = importlib.util.spec_from_file_location(name, source)
            if spec is None or spec.loader is None:
                raise RuntimeError("Cannot load Codex Telegram helpers")
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            try:
                # A running gateway can reload this adapter after the shared
                # helpers change; execute the exact hashed snapshot, not stale bytecode.
                exec(compile(source_bytes, str(source), "exec"), module.__dict__)
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

    @staticmethod
    def _session_name(session_id: str) -> str:
        """Read the current profile's title without creating or changing its DB."""
        fallback = f"Hermes · {session_id[:8]}" if session_id else "Hermes"
        if not session_id:
            return fallback
        try:
            from hermes_constants import get_hermes_home

            path = (get_hermes_home() / "state.db").resolve()
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.2)) as db:
                row = db.execute("SELECT title FROM sessions WHERE id = ?", (session_id,)).fetchone()
            title = " ".join(str(row[0] or "").split()) if row else ""
            return title[:100] or fallback
        except (ImportError, OSError, sqlite3.Error):
            return fallback

    def on_response(self, *, session_id: str = "", turn_id: str = "", assistant_response: str = "", platform: str = "", **_kwargs: Any) -> None:
        if platform == "subagent" or not self._local(platform) or not turn_id or not isinstance(assistant_response, str):
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
        if not self._local(platform) or (platform == "subagent" and not interrupted):
            return
        try:
            common = _bridge_common()
            marker = common.session_emoji(session_id)
            session_name = self._session_name(session_id)
            if interrupted:
                self.send("현재 Hermes 작업이 중단되었습니다.", f"{marker} 🚨 Hermes 중단 · {session_name}")
            elif completed and not failed and response.strip():
                self.send(response[:3800], f"{marker} ✅ Hermes 완료 · {session_name}")
        except Exception:
            # A notification never changes the agent's turn outcome.
            return

    def ask_user(self, args: dict[str, Any], **kwargs: Any) -> str:
        questions = args.get("questions")
        if not isinstance(questions, list) or not 1 <= len(questions) <= 5:
            return json.dumps({"error": "questions must contain 1–5 items"})
        questions = list(questions)
        converted = []
        for index, item in enumerate(questions, 1):
            if not isinstance(item, dict) or not isinstance(item.get("question"), str) or not item["question"].strip():
                return json.dumps({"error": "each question needs text"})
            choices = item.get("choices")
            choices = [] if choices is None else choices
            if not isinstance(choices, list) or any(not isinstance(choice, str) for choice in choices):
                return json.dumps({"error": "choices must be strings"})
            cleaned = [choice.strip() for choice in choices if choice.strip()]
            if choices and not cleaned:
                return json.dumps({"error": "choices must contain non-empty labels"})
            choices = cleaned
            item = {**item, "choices": choices}
            questions[index - 1] = item
            converted.append({
                "header": str(item.get("header") or f"질문 {index}"),
                "question": item["question"],
                "options": [{"label": choice} for choice in choices],
                "multiple": item.get("multi_select") is True and bool(choices),
                "custom": True,
            })
        session_id = str(kwargs.get("session_id") or kwargs.get("task_id") or uuid.uuid4().hex)
        stage = "initialization"
        try:
            _bridge_common().normalize_questions(converted)
            terminal = self._terminal()
            if getattr(terminal, "_single_query_mode", False) is True:
                terminal = None
            terminal_session = self._terminal_session_id(terminal)
            if terminal_session:
                if kwargs.get("session_id") and str(kwargs["session_id"]) != terminal_session:
                    terminal = None
                elif not kwargs.get("session_id"):
                    session_id = terminal_session
            ask_kwargs = dict(
                session_id=session_id,
                session_name=self._session_name(session_id),
                timeout_seconds=min(max(int(args.get("timeout_seconds", 3600)), 1), 86400),
                request_id=f"hermes-question-{uuid.uuid4().hex}",
                agent_name="Hermes",
            )
            if terminal is not None:
                stage = "terminal"
                answers = self._ask_with_terminal(terminal, questions, converted, ask_kwargs)
            else:
                viewer = self._gateway_viewer(session_id)
                if viewer is not None:
                    stage = "terminal"
                    answers = self._ask_with_viewer(viewer, questions, converted, ask_kwargs)
                else:
                    stage = "telegram"
                    answers = self.ask(converted, server_url=_bridge_common().bridge_url(), **ask_kwargs)
            if isinstance(answers, dict):
                payload = {"answers": answers["answers"]}
                if answers.get("outcome") not in (None, "submitted"):
                    payload["outcome"] = answers["outcome"]
            else:
                payload = {"answers": answers}
            return json.dumps(payload, ensure_ascii=False)
        except Exception as exc:
            return json.dumps(self._question_failure(exc, stage), ensure_ascii=False)

    @staticmethod
    def _question_failure(exc: Exception, stage: str) -> dict[str, str]:
        # Only bridge-owned errors have user-facing messages. Arbitrary
        # exceptions may contain credentials, request bodies, or private data.
        detail = type(exc).__name__
        try:
            common = _bridge_common()
            if isinstance(exc, common.TelegramBridgeError):
                detail = " ".join(common.redact_sensitive_text(exc).splitlines())[:500]
        except Exception:
            pass
        _LOG.warning("Hermes Telegram question failed at %s: %s", stage, detail)
        return {
            "error": "Telegram question unavailable; use Hermes native clarify",
            "stage": stage,
            "detail": detail,
        }

    @staticmethod
    def _terminal() -> Any:
        """Use the current thread's CLI owner, never stdin or a global session."""
        try:
            from tools.terminal_tool import _get_approval_callback

            owner = getattr(_get_approval_callback(), "__self__", None)
            if callable(getattr(owner, "_clarify_callback", None)):
                return owner
        except ImportError:
            pass
        return None

    @staticmethod
    def _terminal_session_id(terminal: Any) -> str | None:
        # The agent may rotate its ID during legacy CLI context compression.
        for owner in (getattr(terminal, "agent", None), terminal):
            session_id = getattr(owner, "session_id", None)
            if isinstance(session_id, str) and session_id:
                return session_id
        return None

    @staticmethod
    def _gateway_viewer(session_id: str) -> Any:
        """Use only the current local admission's viewer, never a process-wide CLI."""
        try:
            from gateway.config import Platform
            from gateway.session_finite import finite_turn_required
            from gateway.session_ingress import viewer_turn
        except ImportError:
            return None
        if finite_turn_required() is True:
            return None
        live = viewer_turn.get()
        agent = None
        runner_ref = getattr(sys.modules.get("gateway.run"), "_gateway_runner_ref", None)
        runner = runner_ref() if callable(runner_ref) else None
        if runner is not None:
            from gateway.session_authorities import authority_for_home
            from hermes_constants import get_hermes_home

            authority = authority_for_home(runner, get_hermes_home())
            owned = authority.sessions.get(session_id) if authority is not None else None
            if owned is None or (live is not None and live is not owned):
                return None
            live = owned
            agent = authority.adopted.get(session_id, (None, None))[1]
        if live is None or live.source.platform != Platform.LOCAL or live.source.chat_id != session_id:
            return None
        if not live.event_stream.fanout.transports():
            return None
        return live, agent

    @staticmethod
    def _terminal_questions(questions: list) -> list:
        return [{
            "qid": f"q{index}", "question": item["question"],
            "choices": item.get("choices") or None,
            "choices_offered": item.get("choices") or None,
            "multi_select": item.get("multi_select") is True and bool(item.get("choices")),
        } for index, item in enumerate(questions)]

    @staticmethod
    def _terminal_answers(normalized: list, reply: dict) -> list:
        answers = []
        for question in normalized:
            value = reply.get("answers", {}).get(question["qid"])
            if question["multi_select"] and isinstance(value, str):
                try:
                    parsed = json.loads(value)
                    if isinstance(parsed, list):
                        value = parsed
                except json.JSONDecodeError:
                    pass
            answers.append(value if isinstance(value, list) else ([value] if value is not None else []))
        return answers

    def _ask_with_viewer(self, viewer: Any, questions: list, converted: list, ask_kwargs: dict) -> list | dict:
        """Race Telegram with the gateway's own CLI/TUI clarify request."""
        from gateway.session_server_requests import ask_viewer_result
        from tools.clarify_gateway import get_clarify_timeout

        live, agent = viewer
        normalized = self._terminal_questions(questions)
        cancel = threading.Event()
        completed: queue.Queue = queue.Queue()
        timeout = ask_kwargs["timeout_seconds"]
        native_timeout = get_clarify_timeout()
        if native_timeout > 0:
            native_timeout = min(native_timeout, timeout)
        else:
            native_timeout = timeout

        def interrupted() -> bool:
            return bool(getattr(agent, "_interrupt_requested", False))

        def native() -> Any:
            wire = [{key: item[key] for key in ("qid", "question", "choices", "multi_select")
                     if item[key] is not None} for item in normalized]
            reply = ask_viewer_result(
                live.event_stream, ask_kwargs["session_id"], "clarify", {"questions": wire},
                cancelled=lambda: cancel.is_set() or interrupted(), timeout=native_timeout,
                qids=[item["qid"] for item in normalized],
            )
            return ({"answers": self._terminal_answers(normalized, reply),
                     "outcome": reply.get("outcome", "submitted")} if reply is not None else None)

        def telegram() -> Any:
            return self.ask(
                converted, server_url=_bridge_common().bridge_url(), **ask_kwargs,
                cancel_event=cancel, cancel_message="✅ Hermes 터미널에서 답변을 받았습니다.",
            )

        def receive(source: str, callback: Callable) -> None:
            try:
                completed.put((source, callback()))
            except Exception as exc:
                completed.put((source, exc))

        workers = [threading.Thread(
            target=contextvars.copy_context().run, args=(receive, source, callback), daemon=True,
        ) for source, callback in (("terminal", native), ("telegram", telegram))]
        for worker in workers:
            worker.start()
        deadline = time.monotonic() + timeout
        errors = []
        remaining = len(workers)
        try:
            while remaining and time.monotonic() < deadline:
                if interrupted():
                    cancel.set()
                try:
                    source, result = completed.get(timeout=min(0.1, max(deadline - time.monotonic(), 0.001)))
                except queue.Empty:
                    continue
                remaining -= 1
                if isinstance(result, Exception):
                    self._question_failure(result, source)
                    errors.append(result)
                elif result is not None:
                    return result
            if interrupted():
                return {"answers": [[] for _ in normalized], "outcome": "cancelled"}
            if errors:
                raise errors[0]
            raise TimeoutError("Hermes question timed out")
        finally:
            cancel.set()
            for worker in workers:
                worker.join(timeout=2)

    def _ask_with_terminal(self, terminal: Any, questions: list, converted: list, ask_kwargs: dict) -> list | dict:
        normalized = self._terminal_questions(questions)
        cancel = threading.Event()
        previous = getattr(terminal, "_clarify_state", None)

        def receive() -> None:
            # Capture this modal's identity before sending; a late reply must
            # never resolve a subsequent question in the same CLI.
            while not cancel.wait(0.01):
                state = getattr(terminal, "_clarify_state", None)
                if state is not None and state is not previous:
                    break
            else:
                return
            try:
                answers = self.ask(
                    converted, server_url=_bridge_common().bridge_url(),
                    **ask_kwargs, cancel_event=cancel,
                    cancel_message="✅ Hermes 터미널에서 답변을 받았습니다.",
                )
                if cancel.is_set() or answers is None or len(answers) != len(normalized):
                    return

                def resolve() -> None:
                    if cancel.is_set() or getattr(terminal, "_clarify_state", None) is not state:
                        return
                    if not state["response_queue"].empty():
                        return
                    state["answers"] = {
                        q["qid"]: (answer if q["multi_select"] else (answer[0] if answer else None))
                        for q, answer in zip(normalized, answers)
                    }
                    terminal._clarify_teardown()
                    state["response_queue"].put(True)

                if getattr(terminal, "_app", None) is not None:
                    from hermes_cli.cli_terminal_mixin import _run_on_app_loop

                    _run_on_app_loop(terminal._app, resolve)
                else:
                    resolve()
            except Exception as exc:
                # Telegram failure does not close the still-answerable CLI UI.
                self._question_failure(exc, "telegram")
                return

        worker = threading.Thread(target=contextvars.copy_context().run, args=(receive,), daemon=True)
        worker.start()
        try:
            reply = terminal._clarify_callback(normalized)
            return {"answers": self._terminal_answers(normalized, reply),
                    "outcome": reply.get("outcome", "submitted")}
        finally:
            cancel.set()
            worker.join(timeout=2)

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

    def mirror_clarify(self, *, tool_name: str, args: dict, next_call: Callable, **kwargs: Any) -> Any:
        """Mirror ordinary native clarify calls while retaining Hermes's result contract."""
        if tool_name != "clarify":
            return next_call(args)
        terminal = self._terminal()
        if terminal is not None:
            owner = self._terminal_session_id(terminal)
            if (getattr(terminal, "_single_query_mode", False) is True
                    or (owner and kwargs.get("session_id") and owner != str(kwargs["session_id"]))):
                return next_call(args)
        elif self._gateway_viewer(str(kwargs.get("session_id") or "")) is None:
            # Preserve unattended/subagent and messaging-native clarify policy.
            return next_call(args)
        from tools.clarify_tool import clarify_tool

        def ask(normalized: list) -> dict:
            questions = [{"question": item["question"], "choices": item.get("choices"),
                          "multi_select": item["multi_select"]} for item in normalized]
            reply = json.loads(self.ask_user({"questions": questions}, **kwargs))
            if "error" in reply:
                raise RuntimeError("Telegram question adapter unavailable")
            answers = reply.get("answers")
            if not isinstance(answers, list) or len(answers) != len(normalized):
                raise ValueError("Question adapter returned an invalid answer batch")
            mapped = {}
            for item, values in zip(normalized, answers):
                if not isinstance(values, list):
                    raise ValueError("Question adapter returned an invalid answer")
                mapped[item["qid"]] = (json.dumps(values, ensure_ascii=False) if item["multi_select"]
                                       else (values[0] if values else None))
            return {"answers": mapped, "outcome": reply.get("outcome", "submitted")}

        result = clarify_tool(args.get("questions"), callback=ask)
        return next_call(args) if "error" in json.loads(result) else result

    @staticmethod
    def question_guidance(**_kwargs: Any) -> str:
        return (
            "일반 사용자 질문은 hermes_telegram_ask_user 도구를 사용해 Telegram과 "
            "터미널에 함께 표시하세요. 질문은 한 번에 최대 5개입니다. "
            "대화형 CLI에서는 터미널에서도 선택할 수 있고 먼저 제출된 답변을 사용합니다. "
            "전송에 실패하면 Hermes 기본 clarify를 사용하세요. "
            "비밀번호, API 키, 인증 코드와 로그인은 Hermes의 기본 보안 입력 절차를 사용하세요. "
            "위험한 명령의 승인은 도구 질문이 아니라 Hermes 승인 절차를 따르세요."
        )


def register(ctx: Any) -> None:
    adapter = HermesTelegramAdapter()
    ctx.register_hook("post_llm_call", adapter.on_response)
    ctx.register_hook("on_session_end", adapter.on_end)
    ctx.register_hook("pre_llm_call", adapter.question_guidance)
    register_middleware = getattr(ctx, "register_middleware", None)
    if callable(register_middleware):
        register_middleware("tool_execution", adapter.mirror_clarify)
    ctx.register_approval_transport("codex-telegram", adapter.approve)
    ctx.register_tool(
        name="hermes_telegram_ask_user",
        toolset="hermes_telegram",
        description="Ask the user through the Codex Telegram plugin bot; do not use for command approvals.",
        schema={
            "name": "hermes_telegram_ask_user",
            "description": "Ask up to five ordinary questions in Telegram and the current Hermes terminal. For approvals and secrets use the native safety gate.",
            "parameters": {
                "type": "object",
                "properties": {
                    "questions": {
                        "type": "array", "minItems": 1, "maxItems": 5,
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
