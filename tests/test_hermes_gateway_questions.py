from __future__ import annotations

import contextvars
import json
import threading
import time
import types
import unittest
from unittest import mock

from test_hermes_plugin import load_plugin


class HermesGatewayQuestionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            from gateway.config import Platform
            from gateway.session_events import SessionEvents
            from gateway.session_ingress import viewer_turn
            from gateway import session_server_requests
        except ImportError:
            raise unittest.SkipTest("Run in the Hermes environment for gateway question integration")
        cls.platform = Platform.LOCAL
        cls.events_type = SessionEvents
        cls.viewer_context = viewer_turn
        cls.requests = session_server_requests

    def make_viewer(self, on_question=None):
        ready = threading.Event()
        frames = []

        class Peer:
            def write(self, frame):
                frames.append(frame)
                if frame.get("method") == "clarify":
                    ready.set()
                    if on_question is not None:
                        on_question(frame)
                return True

            def close(self):
                pass

        events = self.events_type()
        events.fanout.attach(Peer())
        self.addCleanup(events.fanout.close)
        live = types.SimpleNamespace(
            source=types.SimpleNamespace(platform=self.platform, chat_id="session-1"),
            event_stream=events, task=None,
        )
        return live, frames, ready

    def run_question(self, adapter, live, questions, *, timeout_seconds=3, **kwargs):
        token = self.viewer_context.set(live)
        try:
            with mock.patch.object(adapter, "_terminal", return_value=None):
                return json.loads(adapter.ask_user({"questions": questions, "timeout_seconds": timeout_seconds},
                                                    session_id="session-1", **kwargs))
        finally:
            self.viewer_context.reset(token)

    def test_question_budget_alone_controls_viewer_deadline(self) -> None:
        from tools import clarify_gateway

        for budget in (0, 2):
            with self.subTest(budget=budget):
                plugin = load_plugin()
                live, frames, ready = self.make_viewer()
                cancelled = threading.Event()

                def telegram(_questions, **kwargs):
                    self.assertEqual(kwargs["timeout_seconds"], budget)
                    self.assertTrue(ready.wait(1))
                    time.sleep(0.35)
                    frame = next(f for f in frames if f.get("method") == "clarify")
                    self.assertTrue(self.requests.resolve({
                        "id": frame["id"], "result": {"answers": {"q0": "native"}, "outcome": "submitted"},
                    }))
                    if kwargs["cancel_event"].wait(1):
                        cancelled.set()
                    return None

                adapter = plugin.HermesTelegramAdapter(ask=telegram)
                native_request = self.requests.ask_viewer_result
                with (mock.patch.object(clarify_gateway, "get_clarify_timeout", return_value=0.01),
                      mock.patch.object(self.requests, "ask_viewer_result", wraps=native_request) as native):
                    result = adapter._ask_with_viewer((live, None), [{"question": "Question?"}], [], {
                        "session_id": "session-1", "timeout_seconds": budget,
                    })
                self.assertEqual(result, {"answers": [["native"]], "outcome": "submitted"})
                self.assertEqual(native.call_args.kwargs["timeout"], budget or None)
                self.assertTrue(cancelled.is_set())
                self.assertFalse(self.requests.open_requests(live.event_stream, "session-1"))

    def test_gateway_question_is_shown_to_the_attached_terminal(self) -> None:
        plugin = load_plugin()
        live, frames, ready = self.make_viewer()

        def telegram(_questions, **_kwargs):
            ready.wait(0.3)
            return [["예"]]

        adapter = plugin.HermesTelegramAdapter(ask=telegram)
        result = self.run_question(adapter, live, [{"question": "진행?", "choices": ["예", "아니오"]}], timeout_seconds=0)
        self.assertEqual(result, {"answers": [["예"]]})
        requests = [frame for frame in frames if frame.get("method") == "clarify"]
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["params"]["session_id"], "session-1")
        self.assertEqual(requests[0]["params"]["questions"][0]["question"], "진행?")
        def closed():
            return any(frame.get("params", {}).get("payload", {}).get("id") == requests[0]["id"]
                       for frame in frames if frame.get("params", {}).get("type") == "request.cancel")
        deadline = time.monotonic() + 1
        while not closed() and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(closed())
        self.assertFalse(self.requests.open_requests(live.event_stream, "session-1"))

    def test_real_cli_panel_can_answer_gateway_question_even_when_telegram_fails(self) -> None:
        from hermes_cli.cli_modal_mixin import CLIModalMixin
        from hermes_cli.gateway_agent import clarify_reply
        from tui_gateway.contracts.registry import SERVER_REQUESTS, validate_params

        class Terminal(CLIModalMixin):
            _app = None
            _clarify_state = None

            def _ring_bell(self, *args, **kwargs):
                pass

            def _paint_now(self):
                pass

            def _persist_prompt_summary(self, *args):
                pass

        for telegram_fails in (False, True):
            with self.subTest(telegram_fails=telegram_fails):
                plugin = load_plugin()
                terminal = Terminal()
                cancelled = threading.Event()
                attempted = threading.Event()
                panel_seen = threading.Event()
                failures = []
                owner = contextvars.ContextVar("test_question_owner", default=None)

                def on_question(frame):
                    try:
                        self.assertIsNone(validate_params(SERVER_REQUESTS["clarify"], frame["params"])[1])
                        reply = clarify_reply(terminal._clarify_callback, frame["params"])
                        self.requests.resolve({"jsonrpc": "2.0", "id": frame["id"], "result": reply},
                                              session_ok=lambda sid, events: sid == "session-1"
                                              and events is live.event_stream)
                    except Exception as exc:
                        failures.append(exc)
                        self.requests.resolve({"jsonrpc": "2.0", "id": frame["id"],
                                               "error": {"code": -32603, "message": "test client failed"}})

                live, frames, ready = self.make_viewer(on_question)

                def human():
                    try:
                        deadline = time.monotonic() + 2
                        while terminal._clarify_state is None and time.monotonic() < deadline:
                            time.sleep(0.005)
                        self.assertIsNotNone(terminal._clarify_state)
                        panel_seen.set()
                        self.assertTrue(attempted.wait(1))
                        for answer in ('["A", "B"]', "터미널 입력", "선택"):
                            terminal._clarify_batch_lock(terminal._clarify_state, answer)
                    except Exception as exc:
                        failures.append(exc)

                def telegram(_questions, **kwargs):
                    self.assertEqual(owner.get(), "current-session")
                    self.assertTrue(ready.wait(1))
                    attempted.set()
                    if telegram_fails:
                        raise OSError("Telegram unavailable")
                    if kwargs["cancel_event"].wait(2):
                        cancelled.set()
                    return None

                adapter = plugin.HermesTelegramAdapter(ask=telegram)
                person = threading.Thread(target=human, daemon=True)
                person.start()
                scope = owner.set("current-session")
                request_viewer = self.requests.ask_viewer_result

                def native_request(*args, **kwargs):
                    self.assertEqual(owner.get(), "current-session")
                    return request_viewer(*args, **kwargs)

                try:
                    with (mock.patch.object(plugin._LOG, "warning"),
                          mock.patch.object(self.requests, "ask_viewer_result", side_effect=native_request)):
                        result = self.run_question(adapter, live, [
                            {"question": "복수 선택", "choices": ["A", "B"], "multi_select": True},
                            {"question": "자유 입력"},
                            {"question": "단일 선택", "choices": ["선택", "다른 선택"]},
                        ])
                finally:
                    owner.reset(scope)
                person.join(2)
                self.assertFalse(person.is_alive())
                self.assertEqual(failures, [])
                self.assertTrue(panel_seen.is_set())
                self.assertEqual(result, {"answers": [["A", "B"], ["터미널 입력"], ["선택"]]})
                self.assertIsNone(terminal._clarify_state)
                if not telegram_fails:
                    self.assertTrue(cancelled.is_set())
                self.assertFalse(self.requests.open_requests(live.event_stream, "session-1"))

    def test_a_child_session_cannot_open_its_parents_terminal_panel(self) -> None:
        plugin = load_plugin()
        live, frames, _ready = self.make_viewer()
        adapter = plugin.HermesTelegramAdapter(ask=lambda *a, **kw: [["Telegram"]])
        token = self.viewer_context.set(live)
        try:
            with mock.patch.object(adapter, "_terminal", return_value=None):
                result = json.loads(adapter.ask_user({"questions": [{"question": "입력?"}]},
                                                     session_id="child-session"))
        finally:
            self.viewer_context.reset(token)
        self.assertEqual(result, {"answers": [["Telegram"]]})
        self.assertFalse(frames)

    def test_headless_turn_keeps_telegram_without_an_unanswerable_cli_panel(self) -> None:
        from gateway.session_finite import finite_turn_scope

        plugin = load_plugin()
        live, frames, _ready = self.make_viewer()
        adapter = plugin.HermesTelegramAdapter(ask=lambda *a, **kw: [["Telegram"]])
        with finite_turn_scope(True):
            result = self.run_question(adapter, live, [{"question": "입력?"}])
        self.assertEqual(result, {"answers": [["Telegram"]]})
        self.assertFalse(frames)

    def test_interrupt_closes_the_panel_and_cancels_telegram(self) -> None:
        from gateway import session_authorities

        plugin = load_plugin()
        agent = types.SimpleNamespace(_interrupt_requested=False)
        cancelled = threading.Event()

        def on_question(_frame):
            agent._interrupt_requested = True

        live, frames, ready = self.make_viewer(on_question)

        def telegram(_questions, **kwargs):
            ready.wait(1)
            if kwargs["cancel_event"].wait(2):
                cancelled.set()
            return None

        adapter = plugin.HermesTelegramAdapter(ask=telegram)
        authority = types.SimpleNamespace(sessions={"session-1": live}, adopted={"session-1": (1, agent)})
        runner = object()
        runtime = types.SimpleNamespace(_gateway_runner_ref=lambda: runner)
        with (mock.patch.dict("sys.modules", {"gateway.run": runtime}),
              mock.patch.object(session_authorities, "authority_for_home", return_value=authority)):
            result = self.run_question(adapter, live, [{"question": "입력?"}], timeout_seconds=0)
        self.assertEqual(result, {"answers": [[]], "outcome": "cancelled"})
        self.assertTrue(cancelled.is_set())
        self.assertFalse(self.requests.open_requests(live.event_stream, "session-1"))

    def test_same_session_id_in_another_profile_cannot_receive_the_question(self) -> None:
        from gateway import session_authorities

        plugin = load_plugin()
        live, frames, _ready = self.make_viewer()
        other_live = object()
        authority = types.SimpleNamespace(sessions={"session-1": other_live}, adopted={})
        runtime = types.SimpleNamespace(_gateway_runner_ref=lambda: object())
        adapter = plugin.HermesTelegramAdapter(ask=lambda *a, **kw: [["Telegram"]])
        with (mock.patch.dict("sys.modules", {"gateway.run": runtime}),
              mock.patch.object(session_authorities, "authority_for_home", return_value=authority)):
            result = self.run_question(adapter, live, [{"question": "입력?"}])
        self.assertEqual(result, {"answers": [["Telegram"]]})
        self.assertFalse(frames)

    def test_background_turn_uses_its_own_attached_viewer_for_five_questions(self) -> None:
        from gateway import session_authorities
        from tui_gateway.contracts.registry import SERVER_REQUESTS, validate_params

        plugin = load_plugin()
        live, frames, ready = self.make_viewer()
        authority = types.SimpleNamespace(sessions={"session-1": live}, adopted={})
        runtime = types.SimpleNamespace(_gateway_runner_ref=lambda: object())

        def telegram(_questions, **kwargs):
            self.assertTrue(ready.wait(1))
            return [[str(i)] for i in range(5)]

        adapter = plugin.HermesTelegramAdapter(ask=telegram)
        with (mock.patch.dict("sys.modules", {"gateway.run": runtime}),
              mock.patch.object(session_authorities, "authority_for_home", return_value=authority)):
            result = self.run_question(adapter, None, [{"question": f"질문 {i}"} for i in range(5)])
        self.assertEqual(result, {"answers": [[str(i)] for i in range(5)]})
        question = next(frame for frame in frames if frame.get("method") == "clarify")
        self.assertEqual(len(question["params"]["questions"]), 5)
        self.assertIsNone(validate_params(SERVER_REQUESTS["clarify"], question["params"])[1])


if __name__ == "__main__":
    unittest.main()
