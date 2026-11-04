from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import hook  # noqa: E402


class HookOutputTests(unittest.TestCase):
    def test_lifecycle_guidance_does_not_load_telegram_credentials(self) -> None:
        with (
            mock.patch("common.load_telegram_config", side_effect=AssertionError("No session registry needed")) as load,
            mock.patch.object(hook, "load_runtime_settings", return_value={}),
            mock.patch.object(hook, "emit") as emit,
        ):
            hook.handle_session_start({"session_id": "session-1", "cwd": "/workspace"})
            hook.handle_user_prompt({"session_id": "session-1", "cwd": "/workspace"})
        load.assert_not_called()
        self.assertEqual(emit.call_count, 2)
        self.assertEqual(emit.call_args_list[0].args[0]["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertEqual(emit.call_args_list[1].args[0], {})

    def test_completion_session_name_uses_bridge_session_title(self) -> None:
        with (
            mock.patch.object(
                hook,
                "resolve_session_name",
                return_value="Telegram plugin response",
            ) as resolve,
            mock.patch.object(
                hook,
                "bridge_url",
                return_value="http://127.0.0.1:43991/",
            ),
        ):
            title = hook.completion_session_name(
                "session-123",
                "/workspace/example-project",
            )

        self.assertEqual(title, "Telegram plugin response")
        resolve.assert_called_once_with(
            "session-123",
            fallback="example-project",
            server_url="http://127.0.0.1:43991/",
        )

    def test_completion_session_name_falls_back_to_folder(self) -> None:
        with mock.patch.object(
            hook,
            "resolve_session_name",
            return_value="example-project",
        ):
            title = hook.completion_session_name(
                "session-1",
                "/workspace/example-project",
            )

        self.assertEqual(title, "example-project")

    def test_session_start_prefers_native_questions_before_telegram(self) -> None:
        emitted: list[dict[str, object]] = []
        with (
            mock.patch.object(hook, "load_runtime_settings", return_value={}),
            mock.patch.object(hook, "emit", side_effect=emitted.append),
        ):
            hook.handle_session_start({"session_id": "session-1", "cwd": "/tmp"})

        context = emitted[0]["hookSpecificOutput"]["additionalContext"]
        self.assertIn("native 질문 UI를 우선 사용", context)
        self.assertIn("일반 질문을 표시한 뒤 로컬 답변을 기다리세요", context)
        self.assertIn("Telegram 상단에 실제 세션 제목", context)
        self.assertIn("MCP 진행 메시지", context)

    def test_session_start_routes_choice_questions_to_telegram_when_configured(self) -> None:
        emitted: list[dict[str, object]] = []
        with (
            mock.patch.object(
                hook,
                "load_runtime_settings",
                return_value={"question_routing": "telegram_choices"},
            ),
            mock.patch.object(hook, "emit", side_effect=emitted.append),
        ):
            hook.handle_session_start({"session_id": "session-1", "cwd": "/tmp"})

        context = emitted[0]["hookSpecificOutput"]["additionalContext"]
        self.assertIn("선택지에서 항목을 고르는 사용자 질문", context)
        self.assertIn("ask_user 도구로 보내 Telegram에도 표시", context)
        self.assertIn("전송에 실패하면 Codex native 질문 UI", context)
        self.assertIn("자유 입력 질문은", context)
        self.assertIn("request_user_input_async", context)

    def test_existing_session_gets_choice_routing_on_next_user_prompt(self) -> None:
        emitted: list[dict[str, object]] = []
        with (
            mock.patch.object(
                hook,
                "load_runtime_settings",
                return_value={"question_routing": "telegram_choices"},
            ),
            mock.patch.object(hook, "emit", side_effect=emitted.append),
        ):
            hook.handle_user_prompt({"session_id": "already-open", "cwd": "/tmp"})

        output = emitted[0]["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "UserPromptSubmit")
        self.assertIn("ask_user 도구로 보내 Telegram에도 표시", output["additionalContext"])

    def test_unconfigured_user_prompt_does_not_add_context(self) -> None:
        with (
            mock.patch.object(hook, "load_runtime_settings", return_value={}),
            mock.patch.object(hook, "emit") as emit,
        ):
            hook.handle_user_prompt({"session_id": "already-open", "cwd": "/tmp"})

        emit.assert_called_once_with({})

    def test_permission_output_can_mirror_result_to_codex_ui(self) -> None:
        output = hook.permission_output(
            "allow",
            system_message="Telegram에서 허용했습니다.",
        )

        self.assertEqual(
            output["hookSpecificOutput"]["decision"]["behavior"],
            "allow",
        )
        self.assertEqual(
            output["systemMessage"],
            "Telegram에서 허용했습니다.",
        )

    def test_completion_uses_the_same_session_emoji(self) -> None:
        with (
            mock.patch.object(hook, "completion_session_name", return_value="작업 세션"),
            mock.patch.object(hook, "session_emoji", return_value="🦊") as emoji,
            mock.patch.object(hook, "send_codex_notification") as send,
            mock.patch.object(hook, "emit"),
        ):
            hook.handle_stop(
                {
                    "session_id": "session-1",
                    "cwd": "/workspace/project",
                    "last_assistant_message": "완료했습니다.",
                }
            )

        emoji.assert_called_once_with("session-1")
        send.assert_called_once_with(
            "완료했습니다.",
            title="🦊 ✅ Codex 완료 · 작업 세션",
        )

    def test_ambient_suggestions_do_not_send_completion(self) -> None:
        output = {"suggestions": [{"title": "Task", "description": "Details", "prompt": "Do task", "appId": "notion", "pluginId": None}]}
        with mock.patch.object(hook, "send_codex_notification") as send, mock.patch.object(hook, "emit") as emit:
            hook.handle_stop({"last_assistant_message": json.dumps(output)})
        send.assert_not_called()
        emit.assert_called_once_with({})

    def test_unrelated_json_still_sends_completion(self) -> None:
        message = json.dumps({"suggestions": ["ordinary response"]})
        with mock.patch.object(hook, "send_codex_notification") as send, mock.patch.object(hook, "emit"):
            hook.handle_stop({"last_assistant_message": message})
        send.assert_called_once()

    def test_subagent_stop_does_not_send_completion(self) -> None:
        for metadata in (
            {"hook_event_name": "SubagentStop", "agent_id": "child", "agent_type": "explorer"},
            {"hook_event_name": "Stop", "agent_id": "child", "agent_type": "worker"},
            {"hook_event_name": "SubagentStop"},
            {"source": {"subagent": "review"}},
        ):
            with (
                self.subTest(metadata=metadata),
                mock.patch.object(hook, "completion_session_name") as resolve,
                mock.patch.object(hook, "send_codex_notification") as send,
                mock.patch.object(hook, "emit") as emit,
            ):
                hook.handle_stop({
                    "session_id": "session-1", "last_assistant_message": "하위 작업 완료", **metadata,
                })
                send.assert_not_called()
                resolve.assert_not_called()
                emit.assert_called_once_with({})

    def test_plain_stop_checks_rollout_source_without_agent_id(self) -> None:
        for metadata in (
            {"source": {"subagent": {"thread_spawn": {"parent_thread_id": "parent", "depth": 1}}}},
            {"source": {"subagent": {"other": "guardian"}}},
            {"source": {"subAgent": "compact"}},
            {"source": "cli", "parent_thread_id": "parent"},
        ):
            with self.subTest(metadata=metadata), tempfile.TemporaryDirectory() as directory:
                transcript = Path(directory) / "rollout.jsonl"
                transcript.write_text(json.dumps({"type": "session_meta", "payload": metadata}) + "\n", encoding="utf-8")
                with (
                    mock.patch.object(hook, "send_codex_notification") as send,
                    mock.patch.object(hook, "completion_session_name") as resolve,
                    mock.patch.object(hook, "emit") as emit,
                ):
                    hook.handle_stop({
                        "session_id": "child", "hook_event_name": "Stop",
                        "transcript_path": str(transcript), "last_assistant_message": "하위 작업 완료",
                    })
                send.assert_not_called()
                resolve.assert_not_called()
                emit.assert_called_once_with({})

    def test_root_fork_and_unavailable_transcript_still_notify(self) -> None:
        headers = (
            json.dumps({"type": "session_meta", "payload": {"source": "cli", "forked_from_id": "older-root"}}),
            "invalid JSON",
            json.dumps({"type": "session_meta", "payload": None}),
            json.dumps({"type": "unknown", "payload": {"source": {"subagent": "review"}}}),
            None,
        )
        for header in headers:
            with self.subTest(header=header), tempfile.TemporaryDirectory() as directory:
                transcript = Path(directory) / "rollout.jsonl"
                if header is not None:
                    transcript.write_text(header + "\n", encoding="utf-8")
                with (
                    mock.patch.object(hook, "send_codex_notification") as send,
                    mock.patch.object(hook, "completion_session_name", return_value="메인 세션"),
                    mock.patch.object(hook, "emit"),
                ):
                    hook.handle_stop({
                        "session_id": "root", "transcript_path": str(transcript),
                        "last_assistant_message": "메인 작업 완료",
                    })
                send.assert_called_once()
                self.assertEqual(send.call_args.args[0], "메인 작업 완료")

    def test_interrupt_sends_emergency_notification(self) -> None:
        with (
            mock.patch.object(hook, "completion_session_name", return_value="작업 세션"),
            mock.patch.object(hook, "session_emoji", return_value="🦊"),
            mock.patch.object(hook, "send_codex_notification") as send,
            mock.patch.object(hook, "emit"),
        ):
            hook.handle_interrupt({"session_id": "session-1", "cwd": "/workspace/project"})

        send.assert_called_once_with(
            "현재 Codex 작업이 중단되었습니다.",
            title="🦊 🚨 Codex 중단 · 작업 세션",
        )

    def test_hooks_send_only_final_or_interrupt_notifications(self) -> None:
        config = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        handlers = config["hooks"]["UserPromptSubmit"][0]["hooks"]

        self.assertEqual(len(handlers), 1)
        self.assertTrue(handlers[0]["command"].endswith('hook.py\" user-prompt'))
        interrupt = config["hooks"]["Interrupt"][0]["hooks"][0]
        self.assertTrue(interrupt["command"].endswith('hook.py\" interrupt'))
        self.assertEqual(interrupt["timeout"], 3)


if __name__ == "__main__":
    unittest.main()
