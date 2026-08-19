from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import bridge  # noqa: E402


class BridgeMappingTests(unittest.TestCase):
    def test_message_envelopes_map_codex_items_to_opencode_shape(self) -> None:
        thread = {
            "turns": [
                {
                    "items": [
                        {
                            "type": "userMessage",
                            "content": [{"type": "text", "text": "질문"}],
                        },
                        {"type": "agentMessage", "text": "답변"},
                        {"type": "reasoning", "summary": ["hidden"]},
                    ]
                }
            ]
        }
        self.assertEqual(
            bridge._message_envelopes(thread),
            [
                {
                    "info": {"role": "user"},
                    "parts": [{"type": "text", "text": "질문"}],
                },
                {
                    "info": {"role": "assistant"},
                    "parts": [{"type": "text", "text": "답변"}],
                },
            ],
        )

    def test_status_mapping_is_fail_safe(self) -> None:
        self.assertEqual(bridge._thread_status({"status": {"type": "active"}}), "busy")
        self.assertEqual(bridge._thread_status({"status": {"type": "systemError"}}), "retry")
        self.assertEqual(bridge._thread_status({"status": {"type": "notLoaded"}}), "idle")
        self.assertEqual(
            bridge._thread_status({"status": {"type": "idle"}}, externally_open=True),
            "busy",
        )

    def test_start_work_limits_workspace_write_to_resumed_cwd(self) -> None:
        config = mock.Mock()
        with mock.patch.object(bridge, "load_telegram_config", return_value=config):
            service = bridge.CodexBridge()
        service.open_thread_ids = mock.Mock(return_value=set())
        service.status_map = mock.Mock(return_value={"thread-1": {"type": "idle"}})
        service.rpc.request = mock.Mock(
            side_effect=[
                {"cwd": "/workspace/example-project", "name": "Resumed session"},
                {"turn": {"id": "turn-1"}},
            ]
        )

        service.start_work("thread-1")

        turn_params = service.rpc.request.call_args_list[1].args[1]
        self.assertEqual(
            turn_params["sandboxPolicy"]["writableRoots"],
            ["/workspace/example-project"],
        )
        self.assertEqual(service.thread_titles["thread-1"], "Resumed session")

    def test_http_request_trust_rejects_cross_site_origins(self) -> None:
        handler = object.__new__(bridge.BridgeRequestHandler)
        handler.headers = {
            "Host": "127.0.0.1:43991",
            "Origin": "https://public.example",
            "Sec-Fetch-Site": "cross-site",
        }
        self.assertFalse(handler._trusted_local_request())

        handler.headers = {
            "Host": "127.0.0.1:43991",
            "Origin": "http://127.0.0.1:43991",
            "Sec-Fetch-Site": "same-origin",
        }
        self.assertTrue(handler._trusted_local_request())

    def test_request_user_input_uses_cached_session_title(self) -> None:
        config = mock.Mock()
        with mock.patch.object(bridge, "load_telegram_config", return_value=config):
            service = bridge.CodexBridge()
        service.thread_titles["thread-1"] = "Cached session title"

        with mock.patch.object(bridge, "ask_questions", return_value=[["예"]]) as ask:
            result = service.handle_server_request(
                "item/tool/requestUserInput",
                {
                    "threadId": "thread-1",
                    "questions": [
                        {
                            "id": "confirm",
                            "header": "확인",
                            "question": "진행할까요?",
                            "options": [{"label": "예", "description": "진행"}],
                        }
                    ],
                },
            )

        self.assertEqual(result, {"answers": {"confirm": {"answers": ["예"]}}})
        self.assertEqual(ask.call_args.kwargs["session_name"], "Cached session title")

    def test_completion_notification_uses_session_emoji(self) -> None:
        config = mock.Mock()
        with mock.patch.object(bridge, "load_telegram_config", return_value=config):
            service = bridge.CodexBridge()
        service.get_thread = mock.Mock(
            return_value={"id": "thread-1", "name": "작업 세션"}
        )

        with (
            mock.patch.object(bridge, "session_emoji", return_value="🐼") as emoji,
            mock.patch.object(bridge, "send_codex_notification") as send,
        ):
            service._notify_completion("thread-1", "완료했습니다.")

        emoji.assert_called_once_with("thread-1")
        send.assert_called_once_with(
            "완료했습니다.",
            title="🐼 ✅ Codex 완료 · 작업 세션",
            config=config,
        )


if __name__ == "__main__":
    unittest.main()
