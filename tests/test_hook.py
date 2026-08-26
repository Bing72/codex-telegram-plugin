from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import hook  # noqa: E402


class HookOutputTests(unittest.TestCase):
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
            mock.patch.object(hook, "register_session"),
            mock.patch.object(hook, "emit", side_effect=emitted.append),
        ):
            hook.handle_session_start({"session_id": "session-1", "cwd": "/tmp"})

        context = emitted[0]["hookSpecificOutput"]["additionalContext"]
        self.assertIn("native 질문 UI를 우선 사용", context)
        self.assertIn("일반 질문을 표시한 뒤 로컬 답변을 기다리세요", context)
        self.assertIn("Telegram 상단에 실제 세션 제목", context)
        self.assertIn("MCP 진행 메시지", context)

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
            mock.patch.object(hook, "register_session"),
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

    def test_interrupt_sends_emergency_notification(self) -> None:
        with (
            mock.patch.object(hook, "register_session") as register,
            mock.patch.object(hook, "completion_session_name", return_value="작업 세션"),
            mock.patch.object(hook, "session_emoji", return_value="🦊"),
            mock.patch.object(hook, "send_codex_notification") as send,
            mock.patch.object(hook, "emit"),
        ):
            hook.handle_interrupt({"session_id": "session-1", "cwd": "/workspace/project"})

        register.assert_called_once_with(
            "session-1",
            title=mock.ANY,
            cwd="/workspace/project",
            status="idle",
        )
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
