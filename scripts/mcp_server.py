#!/usr/bin/env python3
"""Dependency-free MCP stdio server exposing Telegram ask_user."""

from __future__ import annotations

import json
import os
import secrets
import sys
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
        "Ask one or more questions in the user's existing Telegram chat and wait "
        "for button or custom-text answers. Prefer Codex's native question UI or a "
        "normal in-session question while the user is present; use this Telegram-only "
        "response channel for remote or fallback questions. The pending question is "
        "also published as MCP progress so it remains visible in the current Codex "
        "session."
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
                "minimum": 30,
                "maximum": 86400,
                "default": 86400,
            },
        },
    },
}


def write_message(message: dict[str, Any]) -> None:
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


def handle(request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    request_id = request.get("id")
    if method == "notifications/initialized":
        return None
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": "codex-telegram",
                    "version": "0.1.0",
                },
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"tools": [TOOL]},
        }
    if method == "tools/call":
        params = request.get("params") or {}
        if not isinstance(params, dict):
            raise ValueError("invalid tool call params")
        if params.get("name") != "ask_user":
            raise ValueError("unknown tool")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ValueError("invalid tool arguments")
        timeout = int(arguments.get("timeout_seconds") or 86400)
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
        request_id_text = f"codex-question-{secrets.token_urlsafe(12)}"
        short_hash = question_short_hash(request_id_text, session_id, server_url)
        token = progress_token(params)
        write_progress(
            token,
            progress=0,
            message=progress_message(
                display_session_name,
                short_hash,
                normalized_questions,
                state="Telegram 답변 대기",
            ),
        )
        try:
            answers = ask_questions(
                normalized_questions,
                session_id=session_id,
                server_url=server_url,
                timeout_seconds=timeout,
                request_id=request_id_text,
                session_name=display_session_name,
            )
        except Exception:
            write_progress(
                token,
                progress=1,
                message=progress_message(
                    display_session_name,
                    short_hash,
                    normalized_questions,
                    state="응답 없이 종료",
                ),
            )
            raise
        write_progress(
            token,
            progress=1,
            message=progress_message(
                display_session_name,
                short_hash,
                normalized_questions,
                state="Telegram 답변을 Codex에 전달함",
            ),
        )
        payload = {"answers": answers}
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": result_text(payload),
        }
    if request_id is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def main() -> int:
    request: dict[str, Any] = {}
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            candidate = json.loads(line)
            request = candidate if isinstance(candidate, dict) else {}
            response = handle(request)
            if response is not None:
                write_message(response)
        except (ValueError, TelegramBridgeError, TimeoutError) as exc:
            write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request.get("id"),
                    "error": {
                        "code": -32000,
                        "message": redact_sensitive_text(exc),
                    },
                }
            )
        except Exception as exc:  # pragma: no cover - last-resort protocol guard
            print(
                f"codex-telegram-mcp internal error: {redact_sensitive_text(exc)}",
                file=sys.stderr,
            )
            write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request.get("id"),
                    "error": {
                        "code": -32603,
                        "message": redact_sensitive_text(exc),
                    },
                }
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
