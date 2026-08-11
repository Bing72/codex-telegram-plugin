from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import mcp_server  # noqa: E402


class McpServerTests(unittest.TestCase):
    def test_ask_user_reports_question_progress_to_current_codex_session(self) -> None:
        notifications: list[dict[str, object]] = []
        request = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "ask_user",
                "_meta": {
                    "progressToken": "progress-1",
                    "threadId": "thread-from-codex",
                },
                "arguments": {
                    "session_id": "model-supplied-session",
                    "questions": [
                        {
                            "header": "확인",
                            "question": "세션에도 이 질문이 보이나요?",
                            "options": [
                                {"label": "보임", "description": "현재 화면에 보입니다."},
                                {"label": "안 보임", "description": "현재 화면에 없습니다."},
                            ],
                        }
                    ],
                },
            },
        }

        with (
            mock.patch.object(
                mcp_server,
                "bridge_url",
                return_value="http://127.0.0.1:43991/",
            ),
            mock.patch.object(
                mcp_server,
                "resolve_session_name",
                return_value="표시 점검 세션",
            ),
            mock.patch.object(
                mcp_server.secrets,
                "token_urlsafe",
                return_value="fixed-request",
            ),
            mock.patch.object(mcp_server, "ask_questions", return_value=[["보임"]]) as ask,
            mock.patch.object(mcp_server, "write_message", side_effect=notifications.append),
        ):
            response = mcp_server.handle(request)

        self.assertEqual(response["result"]["structuredContent"], {"answers": [["보임"]]})
        self.assertEqual(len(notifications), 2)
        first = notifications[0]
        self.assertEqual(first["method"], "notifications/progress")
        self.assertEqual(first["params"]["progressToken"], "progress-1")
        self.assertEqual(first["params"]["progress"], 0)
        self.assertIn("🧵 세션: 표시 점검 세션", first["params"]["message"])
        self.assertIn("세션에도 이 질문이 보이나요?", first["params"]["message"])
        self.assertIn("선택지: 보임, 안 보임", first["params"]["message"])
        self.assertEqual(notifications[1]["params"]["progress"], 1)
        self.assertEqual(ask.call_args.kwargs["session_id"], "thread-from-codex")
        self.assertEqual(ask.call_args.kwargs["session_name"], "표시 점검 세션")
        self.assertTrue(
            ask.call_args.kwargs["request_id"].startswith("codex-question-")
        )

    def test_ask_user_skips_progress_when_client_does_not_offer_a_token(self) -> None:
        request = {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {
                "name": "ask_user",
                "arguments": {
                    "questions": [{"header": "확인", "question": "진행할까요?"}],
                },
            },
        }

        with (
            mock.patch.object(mcp_server, "resolve_session_name", return_value="세션"),
            mock.patch.object(mcp_server, "ask_questions", return_value=[["예"]]),
            mock.patch.object(mcp_server, "write_message") as write,
        ):
            response = mcp_server.handle(request)

        self.assertEqual(response["result"]["structuredContent"], {"answers": [["예"]]})
        write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
