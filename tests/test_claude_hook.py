from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import claude_hook  # noqa: E402
import setup  # noqa: E402

QUESTION_INPUT = {
    "questions": [
        {
            "question": "Which color?",
            "header": "Color",
            "multiSelect": False,
            "options": [
                {"label": "Red", "description": "warm"},
                {"label": "Blue", "description": "cool"},
            ],
        },
        {
            "question": "Which toppings?",
            "header": "Toppings",
            "multiSelect": True,
            "options": [{"label": "Cheese"}, {"label": "Ham"}],
        },
    ]
}


def write_lines(path: Path, *entries: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")


def tool_use(use_id: str, name: str, tool_input: dict) -> dict:
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": use_id, "name": name, "input": tool_input},
    ]}}


def tool_result(use_id: str, content: str = "ok", *, is_error: bool = False, result=None) -> dict:
    block = {"type": "tool_result", "tool_use_id": use_id, "content": content}
    if is_error:
        block["is_error"] = True
    entry = {"type": "user", "message": {"content": [block]}}
    if result is not None:
        entry["toolUseResult"] = result
    return entry


class EmitCapture:
    def __init__(self) -> None:
        self.payloads: list[dict] = []

    def __call__(self, payload: dict) -> None:
        self.payloads.append(payload)


class ClaudeQuestionTests(unittest.TestCase):
    def test_questions_map_to_plugin_format_with_custom_answers(self) -> None:
        questions = claude_hook.claude_questions(QUESTION_INPUT)
        self.assertEqual([q["header"] for q in questions], ["Color", "Toppings"])
        self.assertEqual([q["multiple"] for q in questions], [False, True])
        self.assertTrue(all(q["custom"] for q in questions))
        self.assertEqual(questions[0]["options"][0], {"label": "Red", "description": "warm"})

    def test_answers_are_keyed_by_question_and_joined_for_multi_select(self) -> None:
        answers = claude_hook.claude_answers(QUESTION_INPUT, [["Blue"], ["Cheese", "Ham"]])
        self.assertEqual(answers, {"Which color?": "Blue", "Which toppings?": "Cheese, Ham"})

    def test_telegram_answer_is_returned_as_ask_user_question_input(self) -> None:
        emitted = EmitCapture()
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "t.jsonl"
            write_lines(transcript, tool_use("toolu_1", "AskUserQuestion", QUESTION_INPUT))
            with (
                mock.patch.object(claude_hook, "ask_questions", return_value=[["Red"], ["Ham"]]) as ask,
                mock.patch.object(claude_hook, "emit", emitted),
            ):
                claude_hook.handle_permission_request({
                    "session_id": "s1", "cwd": directory, "transcript_path": str(transcript),
                    "tool_name": "AskUserQuestion", "tool_input": QUESTION_INPUT,
                })
        self.assertEqual(ask.call_args.kwargs["agent_name"], "Claude")
        decision = emitted.payloads[0]["hookSpecificOutput"]["decision"]
        self.assertEqual(decision["behavior"], "allow")
        self.assertEqual(decision["updatedInput"]["questions"], QUESTION_INPUT["questions"])
        self.assertEqual(
            decision["updatedInput"]["answers"],
            {"Which color?": "Red", "Which toppings?": "Ham"},
        )

    def test_terminal_answer_retires_telegram_question_without_decision(self) -> None:
        emitted = EmitCapture()
        closing: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "t.jsonl"
            write_lines(transcript, tool_use("toolu_1", "AskUserQuestion", QUESTION_INPUT))

            def ask(*_args, cancel_event, cancel_message, **_kwargs):
                write_lines(transcript, tool_result(
                    "toolu_1", result={"answers": {"Which color?": "Blue"}},
                ))
                self.assertTrue(cancel_event.wait(5))
                closing.append(cancel_message())
                return None

            with (
                mock.patch.object(claude_hook, "ask_questions", side_effect=ask),
                mock.patch.object(claude_hook, "emit", emitted),
                mock.patch.object(claude_hook, "POLL_SECONDS", 0.01),
            ):
                claude_hook.handle_permission_request({
                    "session_id": "s1", "cwd": directory, "transcript_path": str(transcript),
                    "tool_name": "AskUserQuestion", "tool_input": QUESTION_INPUT,
                })
        self.assertEqual(emitted.payloads, [{}])
        self.assertIn("터미널에서 답변했습니다", closing[0])
        self.assertIn("Which color? → Blue", closing[0])

    def test_telegram_failure_leaves_terminal_dialog_authoritative(self) -> None:
        emitted = EmitCapture()
        with (
            mock.patch.object(
                claude_hook, "ask_questions",
                side_effect=claude_hook.TelegramBridgeError("offline"),
            ),
            mock.patch.object(claude_hook, "emit", emitted),
        ):
            claude_hook.handle_permission_request({
                "session_id": "s1", "tool_name": "AskUserQuestion", "tool_input": QUESTION_INPUT,
            })
        self.assertEqual(emitted.payloads, [{}])


class ClaudeApprovalTests(unittest.TestCase):
    def run_approval(self, reply: str, data: dict | None = None) -> tuple[dict, mock.Mock]:
        emitted = EmitCapture()
        payload = {
            "session_id": "s1", "cwd": "/work", "tool_name": "Bash",
            "tool_input": {"command": "npm test", "description": "Run tests"},
            **(data or {}),
        }
        with (
            mock.patch.object(claude_hook, "request_permission", return_value=reply) as request,
            mock.patch.object(claude_hook, "is_always_allowed", return_value=False),
            mock.patch.object(claude_hook, "remember_always_allowed") as remember,
            mock.patch.object(claude_hook, "emit", emitted),
        ):
            claude_hook.handle_permission_request(payload)
        request.remember = remember
        return emitted.payloads[0], request

    def test_once_allows_and_labels_request_as_claude(self) -> None:
        output, request = self.run_approval("once")
        self.assertEqual(output["hookSpecificOutput"]["decision"], {"behavior": "allow"})
        self.assertEqual(request.call_args.kwargs["agent_name"], "Claude")
        self.assertEqual(request.call_args.kwargs["title"], "Run tests")
        self.assertEqual(request.call_args.kwargs["patterns"], ["npm test"])

    def test_reject_denies(self) -> None:
        output, _request = self.run_approval("reject")
        decision = output["hookSpecificOutput"]["decision"]
        self.assertEqual(decision["behavior"], "deny")
        self.assertIn("Telegram", decision["message"])

    def test_always_reuses_claude_suggestions_for_the_session_only(self) -> None:
        suggestion = {
            "type": "addRules", "behavior": "allow", "destination": "localSettings",
            "rules": [{"toolName": "Bash", "ruleContent": "npm test"}],
        }
        output, request = self.run_approval("always", {"permission_suggestions": [suggestion]})
        decision = output["hookSpecificOutput"]["decision"]
        self.assertEqual(decision["updatedPermissions"], [{**suggestion, "destination": "session"}])
        request.remember.assert_not_called()

    def test_always_without_suggestions_remembers_fingerprint(self) -> None:
        _output, request = self.run_approval("always")
        request.remember.assert_called_once()

    def test_plan_approval_echoes_required_updated_input(self) -> None:
        plan = {"plan": "1. do it"}
        output, request = self.run_approval("once", {"tool_name": "ExitPlanMode", "tool_input": plan})
        self.assertEqual(output["hookSpecificOutput"]["decision"]["updatedInput"], plan)
        self.assertEqual(request.call_args.kwargs["patterns"], ["1. do it"])

    def test_own_mcp_question_tool_is_allowed_without_second_prompt(self) -> None:
        emitted = EmitCapture()
        with (
            mock.patch.object(claude_hook, "request_permission") as request,
            mock.patch.object(claude_hook, "emit", emitted),
        ):
            claude_hook.handle_permission_request({"tool_name": "mcp__codex-telegram__ask_user"})
        request.assert_not_called()
        self.assertEqual(emitted.payloads[0]["hookSpecificOutput"]["decision"], {"behavior": "allow"})

    def test_terminal_rejection_retires_telegram_prompt(self) -> None:
        emitted = EmitCapture()
        closing: list[str] = []
        tool_input = {"command": "rm -rf build"}
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "t.jsonl"
            write_lines(transcript, tool_use("toolu_9", "Bash", tool_input))

            def request(*, cancel_event, cancel_message, **_kwargs):
                write_lines(transcript, tool_result(
                    "toolu_9", "The user doesn't want to proceed. The tool use was rejected.",
                    is_error=True,
                ))
                self.assertTrue(cancel_event.wait(5))
                closing.append(cancel_message())
                raise claude_hook.TelegramBridgeError("Permission request cancelled")

            with (
                mock.patch.object(claude_hook, "request_permission", side_effect=request),
                mock.patch.object(claude_hook, "is_always_allowed", return_value=False),
                mock.patch.object(claude_hook, "emit", emitted),
                mock.patch.object(claude_hook, "POLL_SECONDS", 0.01),
            ):
                claude_hook.handle_permission_request({
                    "session_id": "s1", "transcript_path": str(transcript),
                    "tool_name": "Bash", "tool_input": tool_input,
                })
        self.assertEqual(emitted.payloads, [{}])
        self.assertEqual(closing, ["🖥️ 터미널에서 요청을 거부했습니다."])


class TranscriptWatcherTests(unittest.TestCase):
    def test_ignores_resolved_calls_and_tracks_new_pending_call(self) -> None:
        tool_input = {"command": "ls"}
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "t.jsonl"
            write_lines(
                transcript,
                tool_use("old", "Bash", tool_input),
                tool_result("old"),
                tool_use("other", "Bash", {"command": "pwd"}),
                tool_use("new", "Bash", tool_input),
            )
            watcher = claude_hook.TranscriptWatcher(transcript, "Bash", tool_input)
            self.assertIsNone(watcher.poll())
            self.assertEqual(watcher.target, "new")
            write_lines(transcript, tool_result("other"))
            self.assertIsNone(watcher.poll())
            with transcript.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(tool_result("new", "done"))[:20])
            self.assertIsNone(watcher.poll())
            with transcript.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(tool_result("new", "done"))[20:] + "\n")
            outcome = watcher.poll()
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome.content, "done")

    def test_subagent_transcript_is_followed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "session.jsonl"
            transcript.write_text("")
            sub = Path(directory) / "session" / "subagents" / "agent-abc.jsonl"
            sub.parent.mkdir(parents=True)
            sub.write_text("")
            path = claude_hook.transcript_for({"transcript_path": str(transcript), "agent_id": "abc"})
            self.assertEqual(path, sub)
            self.assertEqual(claude_hook.transcript_for({"transcript_path": str(transcript)}), transcript)

    def test_abort_signal_cancels_telegram_wait(self) -> None:
        watcher = claude_hook.TranscriptWatcher(None, "Bash", {})
        started = threading.Event()

        def work(cancel: threading.Event) -> str:
            started.set()
            cancel.wait(5)
            return "late"

        def abort() -> None:
            started.wait(5)
            claude_hook.ABORTED.set()

        threading.Thread(target=abort).start()
        try:
            source, value = claude_hook.race_with_terminal(work, watcher, poll_seconds=0.01)
        finally:
            claude_hook.ABORTED.clear()
        self.assertEqual((source, value), ("gone", None))


class ClaudeLifecycleTests(unittest.TestCase):
    def test_session_title_prefers_custom_title(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "t.jsonl"
            write_lines(
                transcript,
                {"type": "ai-title", "aiTitle": "Generated"},
                {"type": "custom-title", "customTitle": "Renamed  session"},
                {"type": "ai-title", "aiTitle": "Generated later"},
            )
            self.assertEqual(claude_hook.session_title(str(transcript)), "Renamed session")
            self.assertEqual(
                claude_hook.display_session_name({"cwd": "/w/proj", "session_id": "abcdef1234"}),
                "Claude · proj · abcdef12",
            )

    def test_stop_sends_labeled_completion(self) -> None:
        emitted = EmitCapture()
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "t.jsonl"
            write_lines(transcript, {"type": "ai-title", "aiTitle": "Fix login"})
            with (
                mock.patch.object(claude_hook, "send_codex_notification") as send,
                mock.patch.object(claude_hook, "emit", emitted),
            ):
                claude_hook.handle_stop({
                    "session_id": "s1", "transcript_path": str(transcript),
                    "last_assistant_message": "Done.",
                })
                claude_hook.handle_stop({"session_id": "s1", "last_assistant_message": "  "})
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0], "Done.")
        self.assertIn("✅ Claude 완료 · Fix login", send.call_args.kwargs["title"])

    def test_session_start_guides_native_question_tool(self) -> None:
        emitted = EmitCapture()
        with mock.patch.object(claude_hook, "emit", emitted):
            claude_hook.handle_session_start({})
        context = emitted.payloads[0]["hookSpecificOutput"]["additionalContext"]
        self.assertIn("AskUserQuestion", context)
        self.assertIn("터미널과 Telegram에 동시에", context)

    def test_main_disables_raw_terminal_mirror_and_fails_open(self) -> None:
        stdout = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["claude_hook.py", "stop"]),
            mock.patch.object(sys, "stdin", io.StringIO('{"last_assistant_message": "x"}')),
            mock.patch.object(sys, "stdout", stdout),
            mock.patch.dict(os.environ, {}, clear=False),
            mock.patch.object(claude_hook, "send_codex_notification", side_effect=RuntimeError("boom")),
            mock.patch.object(claude_hook, "install_abort_handlers"),
        ):
            self.assertEqual(claude_hook.main(), 0)
            self.assertEqual(os.environ["CODEX_TELEGRAM_TERMINAL_MIRROR"], "0")
        self.assertEqual(json.loads(stdout.getvalue()), {})


class ClaudeSetupTests(unittest.TestCase):
    def test_hooks_are_merged_idempotently_without_touching_user_hooks(self) -> None:
        user_hook = {"matcher": "Bash", "hooks": [{"type": "command", "command": "my-linter"}]}
        settings = {"model": "opus", "hooks": {"PermissionRequest": [user_hook]}}
        once = setup.claude_hook_settings(settings, "/usr/bin/python3")
        twice = setup.claude_hook_settings(once, "/usr/bin/python3")
        self.assertEqual(once, twice)
        self.assertEqual(twice["model"], "opus")
        groups = twice["hooks"]["PermissionRequest"]
        self.assertEqual(groups[0], user_hook)
        self.assertEqual(len(groups), 2)
        command = groups[1]["hooks"][0]
        self.assertIn("claude_hook.py permission-request", command["command"])
        self.assertGreaterEqual(command["timeout"], 86400)
        self.assertTrue(twice["hooks"]["Stop"][0]["hooks"][0]["async"])
        self.assertEqual(
            set(twice["hooks"]), {"PermissionRequest", "SessionStart", "Stop", "SessionEnd"},
        )

    def test_install_claude_writes_settings_and_preserves_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory)
            path = config / "settings.json"
            path.write_text(json.dumps({"theme": "auto"}))
            path.chmod(0o600)
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config)}):
                self.assertTrue(setup.install_claude())
                first = path.read_text()
                self.assertTrue(setup.install_claude())
            self.assertEqual(path.read_text(), first)
            data = json.loads(first)
            self.assertEqual(data["theme"], "auto")
            self.assertIn("SessionStart", data["hooks"])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_install_claude_refuses_invalid_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text("{not json")
            with (
                mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": directory}),
                self.assertRaises(setup.TelegramBridgeError),
            ):
                setup.install_claude()
            self.assertEqual(path.read_text(), "{not json")

    def test_install_claude_skips_when_claude_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "none"
            with (
                mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(missing)}),
                mock.patch.object(setup.shutil, "which", return_value=None),
            ):
                self.assertFalse(setup.install_claude())
            self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
