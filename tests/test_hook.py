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

    def test_progress_event_extracts_only_matching_commentary(self) -> None:
        event = {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "turn_id": "turn-1",
                "item": {
                    "type": "AgentMessage",
                    "id": "message-1",
                    "phase": "commentary",
                    "content": [{"type": "Text", "text": "분석을 마쳤습니다."}],
                },
            },
        }

        self.assertEqual(
            hook.progress_event(event, turn_id="turn-1"),
            ("message-1", "분석을 마쳤습니다.", False),
        )
        self.assertEqual(
            hook.progress_event(event, turn_id="turn-2"),
            (None, None, False),
        )

    def test_progress_event_stops_without_forwarding_final_answer(self) -> None:
        event = {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "turn_id": "turn-1",
                "item": {
                    "type": "AgentMessage",
                    "id": "message-final",
                    "phase": "final_answer",
                    "content": [{"type": "Text", "text": "완료했습니다."}],
                },
            },
        }

        self.assertEqual(
            hook.progress_event(event, turn_id="turn-1"),
            (None, None, True),
        )

    def test_progress_event_stops_when_turn_is_aborted(self) -> None:
        event = {
            "type": "event_msg",
            "payload": {
                "type": "turn_aborted",
                "turn_id": "turn-1",
                "reason": "interrupted",
            },
        }

        self.assertEqual(
            hook.progress_event(event, turn_id="turn-1"),
            (None, None, True),
        )

    def test_progress_watcher_sends_commentary_once_and_exits_on_final(self) -> None:
        commentary = {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "turn_id": "turn-1",
                "item": {
                    "type": "AgentMessage",
                    "id": "message-1",
                    "phase": "commentary",
                    "content": [{"type": "Text", "text": "테스트를 시작합니다."}],
                },
            },
        }
        final = {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "turn_id": "turn-1",
                "item": {
                    "type": "AgentMessage",
                    "id": "message-final",
                    "phase": "final_answer",
                    "content": [{"type": "Text", "text": "완료했습니다."}],
                },
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "rollout.jsonl"
            transcript.write_text(
                "\n".join(
                    [
                        json.dumps(commentary, ensure_ascii=False),
                        json.dumps(commentary, ensure_ascii=False),
                        json.dumps(final, ensure_ascii=False),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            with (
                mock.patch.object(hook, "completion_session_name", return_value="작업 세션"),
                mock.patch.object(hook, "session_emoji", return_value="🦊"),
                mock.patch.object(hook, "send_codex_notification") as send,
            ):
                hook.watch_progress(
                    {
                        "session_id": "session-1",
                        "turn_id": "turn-1",
                        "transcript_path": str(transcript),
                        "cwd": directory,
                    },
                    poll_interval=0,
                )

        send.assert_called_once_with(
            "테스트를 시작합니다.",
            title="🦊 Codex 진행 · 작업 세션",
            silent=True,
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

    def test_progress_watcher_ignores_subagent_turns(self) -> None:
        with mock.patch.object(hook, "send_codex_notification") as send:
            hook.watch_progress(
                {
                    "session_id": "session-1",
                    "turn_id": "turn-1",
                    "agent_id": "agent-1",
                    "transcript_path": "/does/not/matter",
                    "cwd": "/tmp",
                },
                poll_interval=0,
                file_wait_seconds=0,
            )

        send.assert_not_called()

    def test_user_prompt_hook_starts_async_progress_watcher(self) -> None:
        config = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        handlers = config["hooks"]["UserPromptSubmit"][0]["hooks"]
        progress = next(
            handler for handler in handlers if handler["command"].endswith('hook.py\" progress')
        )

        self.assertIs(progress["async"], True)
        self.assertEqual(progress["timeout"], 86400)


if __name__ == "__main__":
    unittest.main()
