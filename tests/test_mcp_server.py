from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import mcp_server  # noqa: E402


class McpServerTests(unittest.TestCase):
    @staticmethod
    def _call() -> mcp_server.AskUserCall:
        return mcp_server.AskUserCall(
            request_id=7,
            timeout_seconds=5,
            session_id="thread-1",
            server_url="http://127.0.0.1:43991/",
            session_name="표시 점검 세션",
            questions=mcp_server.normalize_questions(
                [
                    {
                        "header": "확인",
                        "question": "세션에도 이 질문이 보이나요?",
                        "options": [
                            {"label": "보임", "description": "현재 화면에 보입니다."},
                            {"label": "안 보임", "description": "현재 화면에 없습니다."},
                        ],
                    }
                ]
            ),
            question_request_id="codex-question-fixed",
            short_hash="question-hash",
            progress="progress-1",
        )

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

    def test_elicitation_form_keeps_options_and_custom_answer(self) -> None:
        form = mcp_server._elicitation_form(self._call().questions, "표시 점검 세션")
        schema = form.params["requestedSchema"]
        choice = schema["properties"]["question_1"]
        self.assertEqual(choice["type"], "string")
        self.assertEqual(
            [option["title"] for option in choice["oneOf"]],
            ["보임", "안 보임", "직접 입력"],
        )
        self.assertEqual(schema["required"], ["question_1"])
        self.assertIn("question_1_custom", schema["properties"])

        selected = mcp_server._decode_elicitation_answers(
            {"result": {"action": "accept", "content": {"question_1": "보임"}}},
            form,
        )
        self.assertEqual(selected, [["보임"]])

        custom_choice = form.questions[0].custom_choice
        self.assertIsNotNone(custom_choice)
        custom = mcp_server._decode_elicitation_answers(
            {
                "result": {
                    "action": "accept",
                    "content": {
                        "question_1": custom_choice,
                        "question_1_custom": "직접 입력한 답변",
                    },
                }
            },
            form,
        )
        self.assertEqual(custom, [["직접 입력한 답변"]])

    def test_elicitation_form_preserves_multiple_and_text_questions(self) -> None:
        questions = mcp_server.normalize_questions(
            [
                {
                    "header": "기능",
                    "question": "적용할 항목을 모두 고르세요.",
                    "options": [
                        {"label": "A", "description": ""},
                        {"label": "B", "description": ""},
                    ],
                    "multiple": True,
                    "custom": False,
                },
                {
                    "header": "사유",
                    "question": "이유를 입력하세요.",
                },
            ]
        )
        form = mcp_server._elicitation_form(questions, "표시 점검 세션")
        properties = form.params["requestedSchema"]["properties"]
        self.assertEqual(properties["question_1"]["type"], "array")
        self.assertEqual(
            [option["const"] for option in properties["question_1"]["items"]["anyOf"]],
            ["A", "B"],
        )
        self.assertEqual(properties["question_2"]["type"], "string")
        self.assertEqual(
            mcp_server._decode_elicitation_answers(
                {
                    "result": {
                        "action": "accept",
                        "content": {"question_1": ["A", "B"], "question_2": "명시 요청"},
                    }
                },
                form,
            ),
            [["A", "B"], ["명시 요청"]],
        )

    def test_runtime_advertises_elicitation_only_after_client_negotiation(self) -> None:
        runtime = mcp_server.McpRuntime()
        messages: list[dict[str, object]] = []
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": mcp_server.ELICITATION_PROTOCOL_VERSION,
                "capabilities": {"elicitation": {"form": {}}},
            },
        }
        with mock.patch.object(mcp_server, "write_message", side_effect=messages.append):
            runtime.dispatch(request)

        self.assertTrue(runtime.supports_elicitation)
        self.assertEqual(
            messages[0]["result"]["protocolVersion"],
            mcp_server.ELICITATION_PROTOCOL_VERSION,
        )
        self.assertEqual(messages[0]["result"]["capabilities"]["elicitation"], {"form": {}})

    def test_runtime_accepts_codex_answer_and_cancels_telegram_wait(self) -> None:
        runtime = mcp_server.McpRuntime()
        telegram_cancelled = threading.Event()

        def telegram(_call: object, cancel_event: threading.Event | None) -> None:
            assert cancel_event is not None
            cancel_event.wait(timeout=1)
            telegram_cancelled.set()
            return None

        def elicit(form: mcp_server.ElicitationForm, **_kwargs: object) -> dict[str, object]:
            return {
                "result": {
                    "action": "accept",
                    "content": {"question_1": "보임"},
                }
            }

        with (
            mock.patch.object(mcp_server, "_telegram_answers", side_effect=telegram),
            mock.patch.object(runtime, "_request_elicitation", side_effect=elicit),
        ):
            source, answers = runtime._answer_via_both_channels(self._call())

        self.assertEqual(source, "codex")
        self.assertEqual(answers, [["보임"]])
        self.assertTrue(telegram_cancelled.wait(timeout=1))

    def test_runtime_accepts_telegram_answer_and_cancels_elicitation_wait(self) -> None:
        runtime = mcp_server.McpRuntime()
        elicitation_cancelled = threading.Event()

        def elicit(
            _form: mcp_server.ElicitationForm,
            *,
            cancel_event: threading.Event,
            **_kwargs: object,
        ) -> dict[str, object]:
            cancel_event.wait(timeout=1)
            if cancel_event.is_set():
                elicitation_cancelled.set()
                raise mcp_server.ElicitationCancelled()
            raise AssertionError("elicitation was not cancelled")

        with (
            mock.patch.object(mcp_server, "_telegram_answers", return_value=[["보임"]]),
            mock.patch.object(runtime, "_request_elicitation", side_effect=elicit),
        ):
            source, answers = runtime._answer_via_both_channels(self._call())

        self.assertEqual(source, "telegram")
        self.assertEqual(answers, [["보임"]])
        self.assertTrue(elicitation_cancelled.wait(timeout=1))

    def test_runtime_routes_elicitation_response_to_its_pending_request(self) -> None:
        runtime = mcp_server.McpRuntime()
        form = mcp_server._elicitation_form(self._call().questions, "표시 점검 세션")
        emitted: list[dict[str, object]] = []
        result: dict[str, object] = {}
        cancel_event = threading.Event()

        def request() -> None:
            result["response"] = runtime._request_elicitation(
                form,
                timeout_seconds=1,
                cancel_event=cancel_event,
            )

        with mock.patch.object(mcp_server, "write_message", side_effect=emitted.append):
            thread = threading.Thread(target=request)
            thread.start()
            deadline = time.monotonic() + 1
            while not emitted and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(emitted)
            outbound = emitted[0]
            self.assertEqual(outbound["method"], "elicitation/create")
            runtime.dispatch(
                {
                    "jsonrpc": "2.0",
                    "id": outbound["id"],
                    "result": {"action": "accept", "content": {"question_1": "보임"}},
                }
            )
            thread.join(timeout=1)

        self.assertFalse(thread.is_alive())
        self.assertEqual(result["response"]["result"]["action"], "accept")


if __name__ == "__main__":
    unittest.main()
