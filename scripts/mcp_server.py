#!/usr/bin/env python3
"""Dependency-free MCP stdio server exposing dual-channel user questions."""

from __future__ import annotations

import json
import os
import queue
import secrets
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from common import (  # noqa: E402
    TelegramBridgeError,
    ask_questions,
    bridge_url,
    normalize_questions,
    question_short_hash,
    redact_sensitive_text,
    resolve_session_name,
    session_title,
)


TOOL = {
    "name": "ask_user",
    "description": (
        "Ask one or more questions in Telegram and, when the current Codex client "
        "supports MCP elicitation, show the same choices in Codex too. The first "
        "answer wins; Telegram remains the fallback when native elicitation is "
        "unavailable."
    ),
    "inputSchema": {
        "type": "object",
        "required": ["questions"],
        "properties": {
            "questions": {
                "type": "array",
                "minItems": 1,
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "required": ["header", "question"],
                    "properties": {
                        "header": {"type": "string"},
                        "question": {"type": "string"},
                        "options": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": ["label"],
                                "properties": {
                                    "label": {"type": "string"},
                                    "description": {"type": "string"},
                                },
                            },
                        },
                        "multiple": {"type": "boolean", "default": False},
                        "custom": {"type": "boolean", "default": True},
                    },
                },
            },
            "session_id": {
                "type": "string",
                "description": "Optional Codex session id for display/correlation.",
            },
            "timeout_seconds": {
                "type": "integer",
                "minimum": 0,
                "maximum": 86400,
                "default": 0,
                "description": "Seconds to wait; 0 waits indefinitely until answered or cancelled.",
            },
        },
    },
}

LEGACY_PROTOCOL_VERSION = "2025-06-18"
ELICITATION_PROTOCOL_VERSION = "2025-11-25"
LATEST_PROTOCOL_VERSION = "2026-07-28"
SUPPORTED_PROTOCOL_VERSIONS = {
    LEGACY_PROTOCOL_VERSION, ELICITATION_PROTOCOL_VERSION, LATEST_PROTOCOL_VERSION
}
CUSTOM_CHOICE_PREFIX = "__codex_telegram_custom_"
_WRITE_LOCK = threading.Lock()


class ElicitationCancelled(RuntimeError):
    """The other question channel supplied an answer first."""


class QuestionCancelled(RuntimeError):
    """The client or user cancelled the complete question."""


@dataclass(frozen=True)
class AskUserCall:
    request_id: str | int | None
    timeout_seconds: int
    session_id: str
    server_url: str
    session_name: str
    questions: list[dict[str, Any]]
    question_request_id: str
    short_hash: str
    progress: str | int | None


@dataclass(frozen=True)
class ElicitationQuestion:
    key: str
    labels: tuple[str, ...]
    multiple: bool
    custom_choice: str | None
    custom_key: str | None


@dataclass(frozen=True)
class ElicitationForm:
    params: dict[str, Any]
    questions: tuple[ElicitationQuestion, ...]


def write_message(message: dict[str, Any]) -> None:
    with _WRITE_LOCK:
        sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def result_text(payload: Any) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, ensure_ascii=False),
            }
        ],
        "structuredContent": payload,
    }


def request_meta(params: dict[str, Any]) -> dict[str, Any]:
    meta = params.get("_meta")
    return meta if isinstance(meta, dict) else {}


def progress_token(params: dict[str, Any]) -> str | int | None:
    token = request_meta(params).get("progressToken")
    if isinstance(token, bool) or not isinstance(token, (str, int)):
        return None
    return token


def progress_message(
    session_name: str,
    short_hash: str,
    questions: list[dict[str, Any]],
    *,
    state: str,
) -> str:
    lines = [f"🧵 세션: {session_name}", f"요청 ID: {short_hash}", ""]
    for index, question in enumerate(questions, start=1):
        lines.append(f"{index}. {question['header']}: {question['question']}")
        labels = [
            str(option.get("label") or "")
            for option in question.get("options") or []
        ]
        if labels:
            lines.append(f"   선택지: {', '.join(labels)}")
    lines.extend(["", f"상태: {state}"])
    return redact_sensitive_text("\n".join(lines))


def write_progress(
    token: str | int | None,
    *,
    progress: int,
    message: str,
) -> None:
    if token is None:
        return
    write_message(
        {
            "jsonrpc": "2.0",
            "method": "notifications/progress",
            "params": {
                "progressToken": token,
                "progress": progress,
                "total": 1,
                "message": message,
            },
        }
    )


def _client_supports_form_elicitation(params: dict[str, Any]) -> bool:
    capabilities = params.get("capabilities")
    if not isinstance(capabilities, dict):
        return False
    elicitation = capabilities.get("elicitation")
    if not isinstance(elicitation, dict):
        return False
    version = params.get("protocolVersion")
    return version in SUPPORTED_PROTOCOL_VERSIONS and (
        "form" in elicitation or not elicitation
    )


def _initialize_response(
    request_id: str | int | None,
    *,
    protocol_version: str = LEGACY_PROTOCOL_VERSION,
    supports_elicitation: bool = False,
) -> dict[str, Any]:
    capabilities: dict[str, Any] = {"tools": {"listChanged": False}}
    if supports_elicitation:
        capabilities["elicitation"] = {"form": {}}
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "protocolVersion": protocol_version,
            "capabilities": capabilities,
            "serverInfo": {
                "name": "codex-telegram",
                "version": "0.1.0",
            },
        },
    }


def _parse_ask_user_call(request: dict[str, Any]) -> AskUserCall:
    request_id = request.get("id")
    params = request.get("params") or {}
    if not isinstance(params, dict):
        raise ValueError("invalid tool call params")
    if params.get("name") != "ask_user":
        raise ValueError("unknown tool")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        raise ValueError("invalid tool arguments")
    timeout = arguments.get("timeout_seconds", 0)
    if type(timeout) is not int or not 0 <= timeout <= 86400:
        raise ValueError("timeout_seconds must be an integer between 0 and 86400")
    meta = request_meta(params)
    session_id = str(
        meta.get("threadId")
        or arguments.get("session_id")
        or os.environ.get("CODEX_THREAD_ID")
        or f"mcp-{os.getpid()}"
    )
    server_url = bridge_url()
    display_session_name = resolve_session_name(
        session_id,
        fallback=session_title(os.getcwd(), session_id),
        server_url=server_url,
    )
    normalized_questions = normalize_questions(arguments.get("questions") or [])
    if len(normalized_questions) > 3:
        raise ValueError("Codex ask_user supports at most three questions per call")
    question_request_id = f"codex-question-{secrets.token_urlsafe(12)}"
    return AskUserCall(
        request_id=request_id if isinstance(request_id, (str, int)) else None,
        timeout_seconds=timeout,
        session_id=session_id,
        server_url=server_url,
        session_name=display_session_name,
        questions=normalized_questions,
        question_request_id=question_request_id,
        short_hash=question_short_hash(question_request_id, session_id, server_url),
        progress=progress_token(params),
    )


def _tool_result(request_id: str | int | None, answers: list[list[str]]) -> dict[str, Any]:
    payload = {"answers": answers}
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": result_text(payload),
    }


def _custom_choice(labels: tuple[str, ...]) -> str:
    suffix = 1
    while f"{CUSTOM_CHOICE_PREFIX}{suffix}__" in labels:
        suffix += 1
    return f"{CUSTOM_CHOICE_PREFIX}{suffix}__"


def _choice_options(question: dict[str, Any], custom_choice: str | None) -> list[dict[str, str]]:
    options = [
        {
            "const": str(option.get("label") or f"옵션 {index + 1}"),
            "title": str(option.get("label") or f"옵션 {index + 1}"),
        }
        for index, option in enumerate(question.get("options") or [])
        if isinstance(option, dict)
    ]
    if custom_choice is not None:
        options.append({"const": custom_choice, "title": "직접 입력"})
    return options


def _elicitation_form(
    questions: list[dict[str, Any]],
    session_name: str,
    *,
    protocol_version: str = ELICITATION_PROTOCOL_VERSION,
) -> ElicitationForm:
    properties: dict[str, Any] = {}
    required: list[str] = []
    mapped: list[ElicitationQuestion] = []
    for index, question in enumerate(questions, start=1):
        key = f"question_{index}"
        labels = tuple(
            str(option.get("label") or f"옵션 {option_index + 1}")
            for option_index, option in enumerate(question.get("options") or [])
            if isinstance(option, dict)
        )
        custom = question.get("custom") is not False
        multiple = question.get("multiple") is True
        custom_choice = _custom_choice(labels) if labels and custom else None
        custom_key = f"{key}_custom" if custom_choice is not None else None
        description = str(question.get("question") or "")
        if labels:
            choices = _choice_options(question, custom_choice)
            if protocol_version == LEGACY_PROTOCOL_VERSION:
                properties[key] = {
                    "type": "string",
                    "title": str(question.get("header") or f"질문 {index}"),
                    "description": description,
                    "enum": [choice["const"] for choice in choices],
                    "enumNames": [choice["title"] for choice in choices],
                }
            elif multiple:
                properties[key] = {
                    "type": "array",
                    "title": str(question.get("header") or f"질문 {index}"),
                    "description": description,
                    "items": {"anyOf": choices},
                    "minItems": 0,
                }
            else:
                properties[key] = {
                    "type": "string",
                    "title": str(question.get("header") or f"질문 {index}"),
                    "description": description,
                    "oneOf": choices,
                }
            required.append(key)
            if custom_key is not None:
                properties[custom_key] = {
                    "type": "string",
                    "title": f"{question.get('header') or f'질문 {index}'} 직접 답변",
                    "description": "직접 입력을 선택한 경우에만 작성하세요.",
                }
        else:
            properties[key] = {
                "type": "string",
                "title": str(question.get("header") or f"질문 {index}"),
                "description": description,
                "minLength": 1,
            }
            required.append(key)
        mapped.append(
            ElicitationQuestion(
                key=key,
                labels=labels,
                multiple=multiple,
                custom_choice=custom_choice,
                custom_key=custom_key,
            )
        )
    return ElicitationForm(
        params={
            **({"mode": "form"} if protocol_version != LEGACY_PROTOCOL_VERSION else {}),
            "message": (
                f"🧵 세션: {session_name}\n\n"
                "Telegram에도 동일한 질문이 표시됩니다. 먼저 제출된 답변만 사용합니다."
            ),
            "requestedSchema": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
        questions=tuple(mapped),
    )


def _decode_elicitation_answers(
    response: dict[str, Any],
    form: ElicitationForm,
) -> list[list[str]] | None:
    result = response.get("result")
    if not isinstance(result, dict) or result.get("action") != "accept":
        return None
    content = result.get("content")
    if not isinstance(content, dict):
        return None
    answers: list[list[str]] = []
    for question in form.questions:
        raw_answer = content.get(question.key)
        selected = raw_answer if question.multiple else [raw_answer]
        if (not isinstance(selected, list) or (not question.multiple and not selected)
                or any(not isinstance(value, str) or not value.strip() for value in selected)):
            raise ValueError("Codex 질문 답변이 비어 있습니다")
        normalized = list(selected)
        allowed = (*question.labels, question.custom_choice)
        if question.labels and any(value not in allowed for value in normalized):
            raise ValueError("알 수 없는 Codex 선택지입니다")
        if question.custom_choice is not None and question.custom_choice in normalized:
            if question.custom_key is None:
                raise ValueError("직접 입력 필드가 없습니다")
            custom = content.get(question.custom_key)
            if not isinstance(custom, str) or not custom.strip():
                raise ValueError("직접 입력을 선택했으면 답변을 작성해야 합니다")
            normalized = [custom.strip() if value == question.custom_choice else value for value in normalized]
        answers.append(normalized)
    return answers


def _telegram_answers(call: AskUserCall, cancel_event: threading.Event | None = None) -> list[list[str]] | None:
    return ask_questions(
        call.questions,
        session_id=call.session_id,
        server_url=call.server_url,
        timeout_seconds=call.timeout_seconds,
        request_id=call.question_request_id,
        session_name=call.session_name,
        cancel_event=cancel_event,
        cancel_message="Codex에서 질문이 종료되었습니다.",
    )


def _report_progress(call: AskUserCall, *, progress: int, state: str) -> None:
    write_progress(
        call.progress,
        progress=progress,
        message=progress_message(
            call.session_name,
            call.short_hash,
            call.questions,
            state=state,
        ),
    )


def handle(
    request: dict[str, Any],
    *,
    protocol_version: str = LEGACY_PROTOCOL_VERSION,
    supports_elicitation: bool = False,
) -> dict[str, Any] | None:
    method = request.get("method")
    request_id = request.get("id")
    if method == "notifications/initialized":
        return None
    if method == "initialize":
        return _initialize_response(
            request_id if isinstance(request_id, (str, int)) else None,
            protocol_version=protocol_version,
            supports_elicitation=supports_elicitation,
        )
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"tools": [TOOL]},
        }
    if method == "tools/call":
        call = _parse_ask_user_call(request)
        _report_progress(call, progress=0, state="Telegram 답변 대기")
        try:
            answers = _telegram_answers(call)
        except Exception:
            _report_progress(call, progress=1, state="응답 없이 종료")
            raise
        if answers is None:
            raise TimeoutError("Telegram question was cancelled")
        _report_progress(call, progress=1, state="Telegram 답변을 Codex에 전달함")
        return _tool_result(call.request_id, answers)
    if request_id is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


class McpRuntime:
    """Dispatch MCP messages while an ask_user call waits on either channel."""

    def __init__(self) -> None:
        self.protocol_version = LEGACY_PROTOCOL_VERSION
        self.supports_elicitation = False
        self._outbound_lock = threading.Lock()
        self._next_outbound_id = 1
        self._outbound_waiters: dict[int, queue.Queue[dict[str, Any]]] = {}
        self._active_calls: dict[str | int, threading.Event] = {}

    def _next_request_id(self) -> int:
        with self._outbound_lock:
            request_id = self._next_outbound_id
            self._next_outbound_id += 1
            return request_id

    def _register_outbound(self, request_id: int) -> queue.Queue[dict[str, Any]]:
        waiter: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self._outbound_lock:
            self._outbound_waiters[request_id] = waiter
        return waiter

    def _remove_outbound(self, request_id: int) -> None:
        with self._outbound_lock:
            self._outbound_waiters.pop(request_id, None)

    def _resolve_outbound(self, message: dict[str, Any]) -> bool:
        if "method" in message or not ("result" in message or "error" in message):
            return False
        request_id = message.get("id")
        if not isinstance(request_id, int) or isinstance(request_id, bool):
            return False
        with self._outbound_lock:
            waiter = self._outbound_waiters.get(request_id)
        if waiter is None:
            return False
        try:
            waiter.put_nowait(message)
        except queue.Full:
            pass
        return True

    def _cancel_elicitation(self, request_id: int, reason: str) -> None:
        write_message(
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": request_id, "reason": reason},
            }
        )

    def _request_elicitation(
        self,
        form: ElicitationForm,
        *,
        timeout_seconds: float,
        cancel_event: threading.Event,
    ) -> dict[str, Any]:
        request_id = self._next_request_id()
        waiter = self._register_outbound(request_id)
        write_message(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "elicitation/create",
                "params": form.params,
            }
        )
        deadline = time.monotonic() + timeout_seconds if timeout_seconds else None
        try:
            while True:
                if cancel_event.is_set():
                    self._cancel_elicitation(
                        request_id,
                        "다른 채널의 답변이 먼저 도착했습니다.",
                    )
                    raise ElicitationCancelled()
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    break
                try:
                    return waiter.get(timeout=0.2 if remaining is None else min(0.2, remaining))
                except queue.Empty:
                    continue
            self._cancel_elicitation(request_id, "질문 응답 시간이 만료되었습니다.")
            raise TimeoutError("Codex elicitation timed out")
        finally:
            self._remove_outbound(request_id)

    def _answer_via_both_channels(
        self, call: AskUserCall, cancel_event: threading.Event | None = None,
    ) -> tuple[str, list[list[str]]]:
        form = _elicitation_form(
            call.questions, call.session_name, protocol_version=self.protocol_version
        )
        completed: queue.Queue[tuple[str, list[list[str]] | Exception]] = queue.Queue()
        telegram_cancel = threading.Event()
        elicitation_cancel = threading.Event()
        completed_workers = {"telegram": threading.Event(), "codex": threading.Event()}

        def telegram_worker() -> None:
            try:
                answers = _telegram_answers(call, telegram_cancel)
                if answers is not None:
                    completed.put(("telegram", answers))
            except Exception as exc:
                completed.put(("telegram", exc))
            finally:
                completed_workers["telegram"].set()

        def codex_worker() -> None:
            try:
                response = self._request_elicitation(
                    form,
                    timeout_seconds=call.timeout_seconds,
                    cancel_event=elicitation_cancel,
                )
                result = response.get("result")
                if isinstance(result, dict) and result.get("action") == "cancel":
                    raise QuestionCancelled("Codex user cancelled the question")
                answers = _decode_elicitation_answers(response, form)
                if answers is not None:
                    completed.put(("codex", answers))
            except ElicitationCancelled:
                return
            except Exception as exc:
                completed.put(("codex", exc))
            finally:
                completed_workers["codex"].set()

        threading.Thread(target=telegram_worker, name="telegram-question", daemon=True).start()
        threading.Thread(target=codex_worker, name="codex-elicitation", daemon=True).start()
        deadline = time.monotonic() + call.timeout_seconds if call.timeout_seconds else None
        errors: list[Exception] = []
        try:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise QuestionCancelled("Codex client cancelled the question")
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    break
                try:
                    source, outcome = completed.get(
                        timeout=0.2 if remaining is None else min(0.2, remaining)
                    )
                except queue.Empty:
                    if all(event.is_set() for event in completed_workers.values()):
                        break
                    continue
                if isinstance(outcome, QuestionCancelled):
                    raise outcome
                if isinstance(outcome, list):
                    return source, outcome
                errors.append(outcome)
            if errors:
                raise errors[-1]
            raise TimeoutError("Telegram and Codex question timed out")
        finally:
            telegram_cancel.set()
            elicitation_cancel.set()

    def _handle_ask_user(
        self, request: dict[str, Any], cancel_event: threading.Event | None = None,
    ) -> None:
        call: AskUserCall | None = None
        cancel_event = cancel_event or threading.Event()
        try:
            if cancel_event.is_set():
                raise QuestionCancelled("Codex client cancelled the question")
            call = _parse_ask_user_call(request)
            if not self.supports_elicitation or (
                self.protocol_version == LEGACY_PROTOCOL_VERSION
                and any(question.get("multiple") is True for question in call.questions)
            ):
                _report_progress(call, progress=0, state="Telegram 답변 대기")
                answers = _telegram_answers(call, cancel_event)
                if answers is None:
                    raise QuestionCancelled("Telegram question was cancelled")
                _report_progress(call, progress=1, state="Telegram 답변을 Codex에 전달함")
                response = _tool_result(call.request_id, answers)
            else:
                _report_progress(call, progress=0, state="Telegram·Codex 선택지 답변 대기")
                source, answers = self._answer_via_both_channels(call, cancel_event)
                state = (
                    "Telegram 답변을 Codex에 전달함"
                    if source == "telegram"
                    else "Codex 선택지 답변을 Telegram 질문과 동기화함"
                )
                _report_progress(call, progress=1, state=state)
                response = _tool_result(call.request_id, answers)
        except QuestionCancelled as exc:
            if call is not None:
                _report_progress(call, progress=1, state="질문 취소")
            response = {
                "jsonrpc": "2.0", "id": request.get("id"),
                "error": {"code": -32800, "message": str(exc)},
            }
        except (ValueError, TelegramBridgeError, TimeoutError) as exc:
            if call is not None:
                _report_progress(call, progress=1, state="응답 없이 종료")
            response = {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "error": {"code": -32000, "message": redact_sensitive_text(exc)},
            }
        except Exception as exc:  # pragma: no cover - last-resort protocol guard
            print(
                f"codex-telegram-mcp internal error: {redact_sensitive_text(exc)}",
                file=sys.stderr,
            )
            response = {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "error": {"code": -32603, "message": redact_sensitive_text(exc)},
            }
        finally:
            client_cancelled = cancel_event.is_set()
            cancel_event.set()
            with self._outbound_lock:
                request_id = request.get("id")
                if type(request_id) in (str, int) and self._active_calls.get(request_id) is cancel_event:
                    self._active_calls.pop(request_id, None)
        if not client_cancelled:
            write_message(response)

    def dispatch(self, request: dict[str, Any]) -> None:
        if self._resolve_outbound(request):
            return
        method = request.get("method")
        if not isinstance(method, str):
            return
        if method == "notifications/cancelled":
            params = request.get("params")
            request_id = params.get("requestId") if isinstance(params, dict) else None
            if type(request_id) in (str, int):
                with self._outbound_lock:
                    cancelled = self._active_calls.get(request_id)
                if cancelled is not None:
                    cancelled.set()
            return
        if method == "initialize":
            params = request.get("params")
            params = params if isinstance(params, dict) else {}
            self.supports_elicitation = _client_supports_form_elicitation(params)
            self.protocol_version = (
                params["protocolVersion"]
                if params.get("protocolVersion") in SUPPORTED_PROTOCOL_VERSIONS
                else LEGACY_PROTOCOL_VERSION
            )
        if method == "tools/call":
            params = request.get("params")
            if isinstance(params, dict) and params.get("name") == "ask_user":
                cancelled = threading.Event()
                request_id = request.get("id")
                if type(request_id) not in (str, int):
                    write_message({"jsonrpc": "2.0", "id": None,
                                   "error": {"code": -32600, "message": "Invalid request id"}})
                    return
                with self._outbound_lock:
                    duplicate = request_id in self._active_calls
                    if not duplicate:
                        self._active_calls[request_id] = cancelled
                if duplicate:
                    write_message({"jsonrpc": "2.0", "id": request_id,
                                   "error": {"code": -32600, "message": "Duplicate active request id"}})
                    return
                threading.Thread(
                    target=self._handle_ask_user,
                    args=(request, cancelled),
                    name="codex-telegram-ask-user",
                    daemon=True,
                ).start()
                return
        try:
            response = handle(
                request,
                protocol_version=self.protocol_version,
                supports_elicitation=self.supports_elicitation,
            )
            if response is not None:
                write_message(response)
        except (ValueError, TelegramBridgeError, TimeoutError) as exc:
            write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request.get("id"),
                    "error": {"code": -32000, "message": redact_sensitive_text(exc)},
                }
            )
        except Exception as exc:  # pragma: no cover - stdio protocol guard
            print(
                f"codex-telegram-mcp internal error: {redact_sensitive_text(exc)}",
                file=sys.stderr,
            )
            write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request.get("id"),
                    "error": {"code": -32603, "message": redact_sensitive_text(exc)},
                }
            )


def main() -> int:
    runtime = McpRuntime()
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            candidate = json.loads(line)
            if isinstance(candidate, dict):
                runtime.dispatch(candidate)
        except json.JSONDecodeError:
            print("codex-telegram-mcp ignored invalid JSON", file=sys.stderr)
        except Exception as exc:  # pragma: no cover - stdio loop guard
            print(
                f"codex-telegram-mcp dispatch error: {redact_sensitive_text(exc)}",
                file=sys.stderr,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
