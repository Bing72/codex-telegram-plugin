#!/usr/bin/env python3
"""Claude Code hook entrypoint.

Claude Code shows its native question and permission dialogs while a
``PermissionRequest`` hook is still running, and closes the dialog when the
hook returns a decision. This hook therefore mirrors each dialog to Telegram
and races the two channels: the first answer wins. A Telegram answer is
returned as the hook decision; a terminal answer is detected from the session
transcript and retires the Telegram message.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from common import (  # noqa: E402
    TelegramBridgeError,
    ask_questions,
    bridge_url,
    clear_session_permissions,
    is_always_allowed,
    normalize_questions,
    permission_fingerprint,
    remember_always_allowed,
    request_permission,
    send_codex_notification,
    session_emoji,
    summarize_tool_input,
)

AGENT_NAME = "Claude"
QUESTION_TOOL = "AskUserQuestion"
PLAN_TOOL = "ExitPlanMode"
# The plugin's own MCP question tool already reaches Telegram and the terminal
# through MCP elicitation; asking for permission to ask would be a duplicate.
OWN_QUESTION_TOOLS = frozenset({"mcp__codex-telegram__ask_user"})
TRANSCRIPT_INITIAL_BYTES = 16 * 1024 * 1024
TRANSCRIPT_READ_BYTES = 4 * 1024 * 1024
TITLE_SCAN_BYTES = 4 * 1024 * 1024
POLL_SECONDS = 0.3

T = TypeVar("T")
# Claude Code terminates a running hook when the turn is interrupted (for
# example after the user rejects a dialog in the terminal).
ABORTED = threading.Event()


def _abort(_signum: int, _frame: Any) -> None:
    ABORTED.set()


def install_abort_handlers() -> None:
    for name in ("SIGTERM", "SIGHUP", "SIGINT"):
        signum = getattr(signal, name, None)
        if signum is not None:
            signal.signal(signum, _abort)


def read_input() -> dict[str, Any]:
    try:
        value = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def emit(payload: dict[str, Any]) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    sys.stdout.flush()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _iter_json_lines(raw: bytes) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def session_title(transcript_path: Any) -> str | None:
    """Return the user-set or generated Claude session title, newest first."""

    if not isinstance(transcript_path, str) or not transcript_path:
        return None
    path = Path(transcript_path)
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            for start in (max(0, size - TITLE_SCAN_BYTES), 0):
                handle.seek(start)
                raw = handle.read()
                if start:
                    raw = raw.split(b"\n", 1)[-1]
                custom = generated = None
                for entry in _iter_json_lines(raw):
                    kind = entry.get("type")
                    if kind == "custom-title":
                        value = entry.get("customTitle") or entry.get("title")
                        custom = value if isinstance(value, str) else custom
                    elif kind == "ai-title":
                        value = entry.get("aiTitle") or entry.get("title")
                        generated = value if isinstance(value, str) else generated
                title = " ".join(str(custom or generated or "").split())
                if title:
                    return title[:100]
                if start == 0:
                    break
    except OSError:
        return None
    return None


def display_session_name(data: dict[str, Any]) -> str:
    title = session_title(data.get("transcript_path"))
    if title:
        return title
    cwd = str(data.get("cwd") or os.getcwd())
    name = Path(cwd).name or cwd or "Claude"
    session_id = str(data.get("session_id") or "")
    return f"Claude · {name} · {session_id[:8]}" if session_id else f"Claude · {name}"


@dataclass(frozen=True)
class TerminalOutcome:
    """A tool_result Claude Code recorded after the terminal dialog resolved."""

    is_error: bool
    content: str
    tool_use_result: Any


class TranscriptWatcher:
    """Follow a Claude transcript until the pending tool call has a result.

    The hook is not cancelled when the user answers the terminal dialog first,
    so the transcript is the authoritative signal that the dialog is gone.
    """

    def __init__(self, path: Path | None, tool_name: str, tool_input: Any) -> None:
        self.path = path
        self.tool_name = tool_name
        self.key = _canonical(tool_input)
        self.target: str | None = None
        self.outcome: TerminalOutcome | None = None
        self._offset: int | None = None
        self._partial = b""
        self._unresolved: dict[str, bool] = {}
        self._results: dict[str, TerminalOutcome] = {}

    @classmethod
    def for_hook(cls, data: dict[str, Any]) -> "TranscriptWatcher":
        return cls(transcript_for(data), str(data.get("tool_name") or ""), data.get("tool_input"))

    def _read_new(self) -> bytes:
        if self.path is None:
            return b""
        try:
            with self.path.open("rb") as handle:
                if self._offset is None:
                    size = os.fstat(handle.fileno()).st_size
                    self._offset = max(0, size - TRANSCRIPT_INITIAL_BYTES)
                    if self._offset:
                        handle.seek(self._offset)
                        skipped = handle.readline()
                        self._offset += len(skipped)
                handle.seek(self._offset)
                raw = handle.read(TRANSCRIPT_READ_BYTES)
        except OSError:
            return b""
        self._offset += len(raw)
        raw = self._partial + raw
        complete, newline, rest = raw.rpartition(b"\n")
        if not newline:
            self._partial = raw
            return b""
        self._partial = rest
        return complete

    def _consume(self, entry: dict[str, Any]) -> None:
        message = entry.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            block_id = block.get("id")
            if block.get("type") == "tool_use" and isinstance(block_id, str):
                if block.get("name") == self.tool_name and block_id not in self._results:
                    self._unresolved[block_id] = _canonical(block.get("input")) == self.key
            elif block.get("type") == "tool_result":
                use_id = block.get("tool_use_id")
                if not isinstance(use_id, str):
                    continue
                self._unresolved.pop(use_id, None)
                raw_content = block.get("content")
                if isinstance(raw_content, list):
                    raw_content = "\n".join(
                        str(item.get("text") or "") for item in raw_content if isinstance(item, dict)
                    )
                self._results[use_id] = TerminalOutcome(
                    is_error=block.get("is_error") is True,
                    content=str(raw_content or ""),
                    tool_use_result=entry.get("toolUseResult"),
                )

    def poll(self) -> TerminalOutcome | None:
        if self.outcome is not None:
            return self.outcome
        for entry in _iter_json_lines(self._read_new()):
            self._consume(entry)
        if self.target is None and self._unresolved:
            exact = [use_id for use_id, matches in self._unresolved.items() if matches]
            self.target = (exact or list(self._unresolved))[-1]
        if self.target is not None and self.target in self._results:
            self.outcome = self._results[self.target]
        return self.outcome


def transcript_for(data: dict[str, Any]) -> Path | None:
    transcript = data.get("transcript_path")
    if not isinstance(transcript, str) or not transcript:
        return None
    path = Path(transcript)
    agent_id = data.get("agent_id")
    if isinstance(agent_id, str) and agent_id and "/" not in agent_id:
        # Subagent tool calls live in <session>/subagents/agent-<id>.jsonl.
        candidate = path.with_suffix("") / "subagents" / f"agent-{agent_id}.jsonl"
        if candidate.is_file():
            return candidate
    return path


def race_with_terminal(
    work: Callable[[threading.Event], T],
    watcher: TranscriptWatcher,
    *,
    poll_seconds: float = POLL_SECONDS,
) -> tuple[str, T | TerminalOutcome | None]:
    """Run a Telegram wait until it answers or the terminal dialog resolves."""

    cancel = threading.Event()
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = work(cancel)
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller
            box["error"] = exc

    worker = threading.Thread(target=run, name="claude-telegram-wait", daemon=True)
    worker.start()
    parent = os.getppid()
    try:
        while worker.is_alive():
            outcome = watcher.poll()
            if outcome is not None:
                cancel.set()
                worker.join(5)
                return "terminal", outcome
            if ABORTED.is_set() or os.getppid() != parent:
                # Claude Code ended the request or exited; nothing is left to answer.
                cancel.set()
                worker.join(5)
                return "gone", None
            worker.join(poll_seconds)
    finally:
        cancel.set()
    if "error" in box:
        raise box["error"]
    return "telegram", box.get("value")


def claude_questions(tool_input: Any) -> list[dict[str, Any]]:
    if not isinstance(tool_input, dict) or not isinstance(tool_input.get("questions"), list):
        raise TelegramBridgeError("AskUserQuestion input has no questions")
    return normalize_questions(
        {
            "header": item.get("header"),
            "question": item.get("question"),
            "options": item.get("options") or [],
            "multiple": item.get("multiSelect") is True,
            # Claude's dialog always offers free text ("Type something").
            "custom": True,
        }
        for item in tool_input["questions"]
        if isinstance(item, dict)
    )


def claude_answers(tool_input: dict[str, Any], answers: list[list[str]]) -> dict[str, str]:
    mapped: dict[str, str] = {}
    for question, answer in zip(tool_input.get("questions") or [], answers):
        if isinstance(question, dict) and isinstance(question.get("question"), str):
            mapped[question["question"]] = ", ".join(answer)
    return mapped


def terminal_answer_text(outcome: TerminalOutcome | None) -> str:
    if outcome is None:
        return "Claude에서 질문이 종료되었습니다."
    if outcome.is_error:
        return "🖥️ 터미널에서 질문이 취소되었습니다."
    lines = ["🖥️ 터미널에서 답변했습니다."]
    result = outcome.tool_use_result
    answers = result.get("answers") if isinstance(result, dict) else None
    if isinstance(answers, dict):
        for index, (question, answer) in enumerate(answers.items(), start=1):
            lines.append(f"{index}. {question} → {answer}")
    return "\n".join(lines)


def terminal_decision_text(outcome: TerminalOutcome | None) -> str:
    if outcome is None:
        return "Claude 권한 요청이 종료되었습니다."
    if outcome.is_error and any(
        word in outcome.content.lower() for word in ("reject", "denied", "deny", "declin")
    ):
        return "🖥️ 터미널에서 요청을 거부했습니다."
    return "🖥️ 터미널에서 요청을 처리했습니다."


def permission_output(behavior: str, **decision: Any) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": behavior, **decision},
        }
    }


def handle_question(data: dict[str, Any]) -> None:
    tool_input = data.get("tool_input")
    try:
        questions = claude_questions(tool_input)
    except TelegramBridgeError:
        emit({})
        return
    assert isinstance(tool_input, dict)
    session_id = str(data.get("session_id") or "claude")
    watcher = TranscriptWatcher.for_hook(data)
    name = display_session_name(data)

    def wait(cancel: threading.Event) -> list[list[str]] | None:
        return ask_questions(
            questions,
            session_id=session_id,
            server_url=bridge_url(),
            session_name=name,
            agent_name=AGENT_NAME,
            cancel_event=cancel,
            cancel_message=lambda: terminal_answer_text(watcher.outcome),
        )

    try:
        source, answers = race_with_terminal(wait, watcher)
    except (TelegramBridgeError, TimeoutError, OSError):
        emit({})
        return
    if source != "telegram" or not isinstance(answers, list):
        emit({})
        return
    emit(permission_output(
        "allow",
        updatedInput={**tool_input, "answers": claude_answers(tool_input, answers)},
    ))


def describe_tool(tool_name: str, tool_input: Any) -> tuple[str, list[str]]:
    if not isinstance(tool_input, dict):
        return summarize_tool_input(tool_input)
    if tool_name == PLAN_TOOL:
        plan = str(tool_input.get("plan") or "").strip()
        return "계획 승인 요청", [plan[:2500]] if plan else []
    for key in ("file_path", "notebook_path", "url", "query", "pattern"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return f"{tool_name}: {value.strip()[:500]}", [value.strip()[:500]]
    return summarize_tool_input(tool_input)


def session_scoped(suggestions: Any) -> list[dict[str, Any]]:
    """Reuse Claude's suggested rules, limited to the current session."""

    if not isinstance(suggestions, list):
        return []
    scoped = []
    for item in suggestions:
        if isinstance(item, dict) and isinstance(item.get("type"), str):
            scoped.append({**item, "destination": "session"})
    return scoped


def handle_approval(data: dict[str, Any]) -> None:
    session_id = str(data.get("session_id") or "claude")
    tool_name = str(data.get("tool_name") or "unknown")
    tool_input = data.get("tool_input")
    allow_extra: dict[str, Any] = {}
    if tool_name == PLAN_TOOL and isinstance(tool_input, dict):
        # Claude Code requires updatedInput for ExitPlanMode approvals.
        allow_extra["updatedInput"] = tool_input
    fingerprint = permission_fingerprint(tool_name, tool_input)
    if is_always_allowed(session_id, fingerprint):
        emit(permission_output("allow", **allow_extra))
        return
    title, patterns = describe_tool(tool_name, tool_input)
    watcher = TranscriptWatcher.for_hook(data)
    suggestions = session_scoped(data.get("permission_suggestions"))

    def wait(cancel: threading.Event) -> str:
        return request_permission(
            session_id=session_id,
            title=title,
            permission=tool_name,
            patterns=patterns,
            directory=str(data.get("cwd") or os.getcwd()),
            cancel_event=cancel,
            agent_name=AGENT_NAME,
            session_name=display_session_name(data),
            cancel_message=lambda: terminal_decision_text(watcher.outcome),
        )

    try:
        source, reply = race_with_terminal(wait, watcher)
    except (TelegramBridgeError, TimeoutError, OSError):
        # No decision: the native terminal dialog stays authoritative.
        emit({})
        return
    if source != "telegram" or reply not in {"once", "always", "reject"}:
        emit({})
        return
    if reply == "reject":
        emit(permission_output("deny", message="Telegram에서 사용자가 거부했습니다."))
        return
    if reply == "always":
        if suggestions:
            allow_extra["updatedPermissions"] = suggestions
        else:
            remember_always_allowed(session_id, fingerprint)
    emit(permission_output("allow", **allow_extra))


def handle_permission_request(data: dict[str, Any]) -> None:
    tool_name = str(data.get("tool_name") or "")
    if tool_name in OWN_QUESTION_TOOLS:
        emit(permission_output("allow"))
    elif tool_name == QUESTION_TOOL:
        handle_question(data)
    else:
        handle_approval(data)


def session_guidance() -> str:
    return (
        "Telegram 연동이 활성화되어 있습니다. 사용자에게 선택이나 결정을 물어야 하면 "
        "일반 텍스트 질문 대신 AskUserQuestion 도구를 사용하세요. AskUserQuestion 질문과 "
        "권한 확인은 현재 터미널과 Telegram에 동시에 표시되고, 먼저 제출된 답변이 "
        "사용됩니다. 같은 질문을 codex-telegram ask_user로 중복 전송하지 마세요. "
        "작업 완료 알림도 같은 Telegram 대화에 전달됩니다."
    )


def handle_session_start(data: dict[str, Any]) -> None:
    emit({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": session_guidance(),
        }
    })


def handle_stop(data: dict[str, Any]) -> None:
    if data.get("hook_event_name") == "SubagentStop":
        emit({})
        return
    message = data.get("last_assistant_message")
    if isinstance(message, str) and message.strip():
        session_id = str(data.get("session_id") or "")
        try:
            send_codex_notification(
                message,
                title=(
                    f"{session_emoji(session_id)} ✅ Claude 완료 · "
                    f"{display_session_name(data)}"
                ),
            )
        except TelegramBridgeError:
            pass
    emit({})


def handle_session_end(data: dict[str, Any]) -> None:
    session_id = str(data.get("session_id") or "")
    if session_id:
        clear_session_permissions(session_id)
    emit({})


HANDLERS: dict[str, Callable[[dict[str, Any]], None]] = {
    "session-start": handle_session_start,
    "permission-request": handle_permission_request,
    "stop": handle_stop,
    "session-end": handle_session_end,
}


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    data = read_input()
    # Claude Code owns the terminal; raw /dev/tty writes would corrupt its UI.
    # Its native dialog is the terminal half of every mirrored request.
    os.environ["CODEX_TELEGRAM_TERMINAL_MIRROR"] = "0"
    handler = HANDLERS.get(mode)
    if handler is None or os.environ.get("CODEX_TELEGRAM_BRIDGE_CHILD") == "1":
        emit({})
        return 0
    install_abort_handlers()
    try:
        handler(data)
    except Exception:
        # Hooks fail open to Claude Code's native dialogs.
        emit({})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
