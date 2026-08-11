#!/usr/bin/env python3
"""Codex lifecycle hook entrypoint."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from common import (  # noqa: E402
    TelegramBridgeError,
    bridge_url,
    clear_session_permissions,
    is_always_allowed,
    permission_fingerprint,
    register_session,
    remember_always_allowed,
    request_permission,
    resolve_session_name,
    send_codex_notification,
    session_title,
    summarize_tool_input,
)


def completion_session_name(session_id: str, cwd: str) -> str:
    fallback = Path(cwd).name or cwd or "Codex"
    return resolve_session_name(
        session_id,
        fallback=fallback,
        server_url=bridge_url(),
    )


def read_input() -> dict[str, Any]:
    try:
        value = json.load(sys.stdin)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def emit(payload: dict[str, Any]) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")


def handle_session_start(data: dict[str, Any]) -> None:
    session_id = str(data.get("session_id") or "")
    cwd = str(data.get("cwd") or os.getcwd())
    if session_id:
        register_session(
            session_id,
            title=session_title(cwd, session_id),
            cwd=cwd,
            status="idle",
        )
    emit(
        {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": (
                    "Telegram 연동이 활성화되어 있습니다. 사용자가 현재 Codex 세션에 "
                    "있으면 native 질문 UI를 우선 사용하고, 그것이 없으면 세션에 한 개의 "
                    "간결한 일반 질문을 표시한 뒤 로컬 답변을 기다리세요. 현재 세션에서 "
                    "답을 받을 수 없거나 사용자가 원격 Telegram 응답을 명시적으로 원할 "
                    "때만 codex-telegram MCP의 ask_user 도구를 사용하세요. ask_user 질문은 "
                    "Telegram 상단에 실제 세션 제목을 표시하고, MCP 진행 메시지와 동일 "
                    "요청 ID로 현재 Codex 세션에도 표시합니다. ask_user를 호출한 경우 실제 "
                    "답변은 Telegram에서 받고 Codex로 자동 전달됩니다. 완료 알림도 같은 "
                    "Telegram 대화에 전달됩니다."
                ),
            }
        }
    )


def handle_user_prompt(data: dict[str, Any]) -> None:
    session_id = str(data.get("session_id") or "")
    cwd = str(data.get("cwd") or os.getcwd())
    if session_id:
        register_session(
            session_id,
            title=session_title(cwd, session_id),
            cwd=cwd,
            status="busy",
        )
    emit({})


def permission_output(
    behavior: str,
    message: str | None = None,
    *,
    system_message: str | None = None,
) -> dict[str, Any]:
    decision: dict[str, str] = {"behavior": behavior}
    if message:
        decision["message"] = message
    output: dict[str, Any] = {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": decision,
        }
    }
    if system_message:
        output["systemMessage"] = system_message
    return output


def handle_permission_request(data: dict[str, Any]) -> None:
    session_id = str(data.get("session_id") or "codex")
    tool_name = str(data.get("tool_name") or "unknown")
    tool_input = data.get("tool_input")
    fingerprint = permission_fingerprint(tool_name, tool_input)
    if is_always_allowed(session_id, fingerprint):
        emit(permission_output("allow"))
        return
    title, patterns = summarize_tool_input(tool_input)
    try:
        reply = request_permission(
            session_id=session_id,
            title=title,
            permission=tool_name,
            patterns=patterns,
            directory=str(data.get("cwd") or os.getcwd()),
        )
    except (TelegramBridgeError, TimeoutError):
        # No decision: Codex falls back to its normal local approval UI.
        emit({})
        return
    if reply == "reject":
        emit(
            permission_output(
                "deny",
                "Telegram에서 사용자가 거부했습니다.",
                system_message="Codex Telegram 권한 요청이 거부되었습니다.",
            )
        )
        return
    if reply == "always":
        remember_always_allowed(session_id, fingerprint)
    emit(
        permission_output(
            "allow",
            system_message=(
                "Codex Telegram에서 동일 요청을 현재 세션 동안 허용했습니다."
                if reply == "always"
                else "Codex Telegram에서 이번 요청을 허용했습니다."
            ),
        )
    )


def handle_stop(data: dict[str, Any]) -> None:
    session_id = str(data.get("session_id") or "")
    cwd = str(data.get("cwd") or os.getcwd())
    if session_id:
        register_session(
            session_id,
            title=session_title(cwd, session_id),
            cwd=cwd,
            status="idle",
        )
    message = data.get("last_assistant_message")
    if isinstance(message, str) and message.strip():
        try:
            send_codex_notification(
                message,
                title=f"Codex 완료 · {completion_session_name(session_id, cwd)}",
            )
        except TelegramBridgeError:
            pass
    emit({})


def handle_session_end(data: dict[str, Any]) -> None:
    session_id = str(data.get("session_id") or "")
    if session_id:
        clear_session_permissions(session_id)
    emit({})


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    data = read_input()
    if os.environ.get("CODEX_TELEGRAM_BRIDGE_CHILD") == "1":
        emit({})
        return 0
    try:
        if mode == "session-start":
            handle_session_start(data)
        elif mode == "user-prompt":
            handle_user_prompt(data)
        elif mode == "permission-request":
            handle_permission_request(data)
        elif mode == "stop":
            handle_stop(data)
        elif mode == "session-end":
            handle_session_end(data)
        else:
            emit({})
    except Exception:
        # Hooks are helpers, not an enforcement boundary. Fail open to Codex's
        # native UI while keeping stdout valid JSON.
        emit({})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
