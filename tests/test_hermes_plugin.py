from __future__ import annotations

import importlib.util
import json
import os
import queue
import sqlite3
import sys
import tempfile
import types
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_FILE = ROOT / "hermes_plugin" / "__init__.py"


def load_plugin():
    spec = importlib.util.spec_from_file_location("codex_telegram_hermes_test", PLUGIN_FILE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class HermesPluginTests(unittest.TestCase):
    def test_missing_credentials_return_actionable_diagnostics(self) -> None:
        plugin = load_plugin()
        common = plugin._bridge_common()
        adapter = plugin.HermesTelegramAdapter()
        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch.object(common, "telegram_env_path", return_value=Path(directory) / "missing.env"),
                mock.patch.object(adapter, "_terminal", return_value=None),
                self.assertLogs(plugin.__name__, level="WARNING") as logs,
            ):
                result = json.loads(adapter.ask_user({"questions": [{"question": "진행?"}]}))
        self.assertIn("use Hermes native clarify", result["error"])
        self.assertIn("Telegram env file not found", result["detail"])
        self.assertEqual(result["stage"], "telegram")
        self.assertIn("Telegram env file not found", logs.output[0])

    def test_telegram_initialization_failure_keeps_terminal_usable(self) -> None:
        plugin = load_plugin()
        common = plugin._bridge_common()
        attempted = threading.Event()

        def broken_url():
            attempted.set()
            raise RuntimeError("private initialization details")

        class Terminal:
            _clarify_state = None

            def _clarify_callback(self, questions):
                self._clarify_state = {"answers": {}, "response_queue": queue.Queue()}
                if not attempted.wait(1):
                    raise TimeoutError("Telegram worker did not start")
                return {"answers": {"q0": "터미널 답변"}, "outcome": "submitted"}

        adapter = plugin.HermesTelegramAdapter(ask=mock.Mock())
        with (
            mock.patch.object(common, "bridge_url", side_effect=broken_url),
            mock.patch.object(adapter, "_terminal", return_value=Terminal()),
            self.assertLogs(plugin.__name__, level="WARNING") as logs,
        ):
            result = json.loads(adapter.ask_user({"questions": [{"question": "진행?"}]}))
        self.assertEqual(result, {"answers": [["터미널 답변"]]})
        self.assertIn("telegram", logs.output[0])
        self.assertNotIn("private initialization details", " ".join(logs.output))
        adapter.ask.assert_not_called()

    def test_real_hermes_cli_modal_resolves_from_either_channel(self) -> None:
        try:
            from hermes_cli.cli_modal_mixin import CLIModalMixin
            from tools.terminal_tool import set_approval_callback
        except ImportError:
            self.skipTest("Run with hermes --run-module unittest for native CLI integration")

        class Terminal(CLIModalMixin):
            _app = None
            _clarify_state = None

            def _approval_callback(self, *args, **kwargs):
                return "deny"

            def _ring_bell(self, *args, **kwargs):
                pass

            def _paint_now(self):
                pass

            def _persist_prompt_summary(self, *args):
                pass

        for failure_at in (None, "delivery", "initialization"):
            with self.subTest(failure_at=failure_at):
                terminal = Terminal()
                plugin = load_plugin()
                common = plugin._bridge_common()
                original_url = common.bridge_url

                def bridge_url():
                    if failure_at == "initialization":
                        terminal._clarify_batch_lock(terminal._clarify_state, "터미널 자유 입력")
                        raise RuntimeError("Telegram initialization unavailable")
                    return original_url()

                def ask(_questions, **kwargs):
                    if failure_at == "delivery":
                        state = terminal._clarify_state
                        terminal._clarify_batch_lock(state, "터미널 자유 입력")
                        raise OSError("Telegram unavailable")
                    return [["A", "B"], ["Telegram 자유 입력"]]

                questions = [{"question": "입력?"}] if failure_at else [
                    {"question": "복수 선택?", "choices": ["A", "B"], "multi_select": True},
                    {"question": "입력?"},
                ]
                set_approval_callback(terminal._approval_callback)
                try:
                    adapter = plugin.HermesTelegramAdapter(ask=ask)
                    self.assertIs(adapter._terminal(), terminal)
                    with mock.patch.object(common, "bridge_url", side_effect=bridge_url):
                        result = json.loads(adapter.ask_user({"questions": questions}))
                finally:
                    set_approval_callback(None)
                expected = [["터미널 자유 입력"]] if failure_at else [["A", "B"], ["Telegram 자유 입력"]]
                self.assertEqual(result, {"answers": expected})
                self.assertIsNone(terminal._clarify_state)

    def test_terminal_multi_select_decodes_native_json_answer(self) -> None:
        plugin = load_plugin()
        terminal = mock.Mock()
        terminal._clarify_callback.return_value = {"answers": {"q0": '["A", "B"]'}, "outcome": "submitted"}
        adapter = plugin.HermesTelegramAdapter(ask=mock.Mock())
        with mock.patch.object(adapter, "_terminal", return_value=terminal):
            result = json.loads(adapter.ask_user({"questions": [{"question": "선택", "choices": ["A", "B"], "multi_select": True}]}))
        self.assertEqual(result, {"answers": [["A", "B"]]})

    def test_late_telegram_callback_does_not_overwrite_terminal_or_next_modal(self) -> None:
        plugin = load_plugin()
        scheduled = threading.Event()
        callbacks = []

        def schedule(_app, callback):
            callbacks.append(callback)
            scheduled.set()

        class Terminal:
            _app = object()
            _clarify_state = None

            def _clarify_callback(self, questions):
                self._clarify_state = {"answers": {}, "response_queue": queue.Queue()}
                if not scheduled.wait(2):
                    raise TimeoutError("No Telegram reply")
                self._clarify_state = {"answers": {"q0": "다음 질문"}}
                return {"answers": {"q0": "터미널"}, "outcome": "submitted"}

        terminal = Terminal()
        adapter = plugin.HermesTelegramAdapter(ask=lambda *a, **kw: [["Telegram"]])
        module = types.SimpleNamespace(_run_on_app_loop=schedule)
        with mock.patch.object(adapter, "_terminal", return_value=terminal), mock.patch.dict(
            sys.modules, {"hermes_cli.cli_terminal_mixin": module}
        ):
            result = json.loads(adapter.ask_user({"questions": [{"question": "입력?"}]}))
            callbacks[0]()
        self.assertEqual(result, {"answers": [["터미널"]]})
        assert terminal._clarify_state is not None
        self.assertEqual(terminal._clarify_state["answers"], {"q0": "다음 질문"})

    def test_terminal_answer_cancels_telegram_wait(self) -> None:
        plugin = load_plugin()
        local_open = threading.Event()
        telegram_started = threading.Event()
        cancelled = threading.Event()

        class Terminal:
            def _clarify_callback(self, questions):
                self._clarify_state = {"answers": {}, "response_queue": queue.Queue()}
                local_open.set()
                if not telegram_started.wait(1):
                    raise TimeoutError("Telegram worker did not start")
                self._clarify_state["answers"] = {"q0": "아니오"}
                return {"answers": {"q0": "아니오"}, "outcome": "submitted"}

        def ask(_questions, **kwargs):
            self.assertTrue(local_open.wait(1))
            telegram_started.set()
            if kwargs["cancel_event"].wait(1):
                cancelled.set()
            return None

        adapter = plugin.HermesTelegramAdapter(ask=ask)
        with mock.patch.object(adapter, "_terminal", return_value=Terminal()):
            result = json.loads(adapter.ask_user({"questions": [{"question": "진행?", "choices": ["예", "아니오"]}]}))
        self.assertEqual(result, {"answers": [["아니오"]]})
        self.assertTrue(cancelled.is_set())

    def test_notifications_use_current_profile_session_title(self) -> None:
        plugin = load_plugin()
        sent = []
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with closing(sqlite3.connect(home / "state.db")) as db, db:
                db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT)")
                db.execute("INSERT INTO sessions VALUES (?, ?)", ("s1", "  Telegram\n제목 구현  "))
            constants = types.SimpleNamespace(get_hermes_home=lambda: home)
            with mock.patch.dict(sys.modules, {"hermes_constants": constants}):
                adapter = plugin.HermesTelegramAdapter(send=lambda text, title: sent.append((text, title)))
                adapter.on_response(session_id="s1", turn_id="t1", assistant_response="완료", platform="cli")
                adapter.on_end(session_id="s1", turn_id="t1", completed=True, platform="cli")
                with closing(sqlite3.connect(home / "state.db")) as db, db:
                    db.execute("UPDATE sessions SET title = ?", ("변경된 제목",))
                adapter.on_end(session_id="s1", turn_id="t2", interrupted=True, platform="cli")
        self.assertTrue(sent[0][1].endswith("✅ Hermes 완료 · Telegram 제목 구현"))
        self.assertTrue(sent[1][1].endswith("🚨 Hermes 중단 · 변경된 제목"))

    def test_completion_and_interrupt_are_distinct_and_not_duplicated(self) -> None:
        plugin = load_plugin()
        sent = []
        adapter = plugin.HermesTelegramAdapter(send=lambda text, title: sent.append((text, title)))
        adapter.on_response(session_id="s1", turn_id="t1", assistant_response="완료", platform="cli")
        adapter.on_end(session_id="s1", turn_id="t1", completed=True, failed=False, interrupted=False, platform="cli")
        adapter.on_end(session_id="s1", turn_id="t1", completed=True, failed=False, interrupted=False, platform="cli")
        adapter.on_end(session_id="s1", turn_id="t2", completed=False, failed=False, interrupted=True, platform="cli")
        adapter.on_end(session_id="s1", turn_id="t2", completed=False, failed=False, interrupted=True, platform="cli")
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0][0], "완료")
        self.assertIn("Hermes 완료", sent[0][1])
        self.assertIn("Hermes 중단", sent[1][1])

    def test_telegram_origin_does_not_echo_its_own_completion(self) -> None:
        plugin = load_plugin()
        sent = mock.Mock()
        adapter = plugin.HermesTelegramAdapter(send=sent)
        adapter.on_response(session_id="s", turn_id="t", assistant_response="이미 전달", platform="telegram")
        adapter.on_end(session_id="s", turn_id="t", completed=True, interrupted=False, platform="telegram")
        sent.assert_not_called()

    def test_approval_returns_only_host_bound_allowed_choice(self) -> None:
        plugin = load_plugin()
        adapter = plugin.HermesTelegramAdapter(
            ask=mock.Mock(return_value=[["현재 세션 동안 허용"]]), send=mock.Mock()
        )
        request = mock.Mock(
            allowed_choices=("once", "session", "deny"),
            request_id="rid", timeout_seconds=30,
            description="명령 허용", command="echo ok", surface="cli",
        )
        request.respond.side_effect = lambda choice: {"bound_choice": choice}
        result = adapter.approve(request)
        self.assertEqual(result, {"bound_choice": "session"})
        self.assertEqual(adapter.ask.call_args.kwargs["agent_name"], "Hermes")
        options = adapter.ask.call_args.args[0][0]["options"]
        self.assertNotIn("항상 허용", [o["label"] for o in options])

    def test_approval_delivery_error_denies(self) -> None:
        plugin = load_plugin()
        adapter = plugin.HermesTelegramAdapter(
            ask=mock.Mock(side_effect=TimeoutError()), send=mock.Mock()
        )
        request = mock.Mock(
            allowed_choices=("once", "deny"), request_id="rid",
            timeout_seconds=5, description="승인", command="run", surface="cli",
        )
        request.respond.side_effect = lambda choice: choice
        self.assertEqual(adapter.approve(request), "deny")

    def test_question_tool_delivers_hermes_branded_choices(self) -> None:
        plugin = load_plugin()
        adapter = plugin.HermesTelegramAdapter(
            ask=mock.Mock(return_value=[["예"]]), send=mock.Mock()
        )
        result = json.loads(adapter.ask_user({
            "questions": [{"question": "진행할까요?", "choices": ["예", "아니오"]}]
        }, task_id="task-1"))
        self.assertEqual(result["answers"], [["예"]])
        self.assertEqual(adapter.ask.call_args.kwargs["agent_name"], "Hermes")
        self.assertIn("Hermes", adapter.ask.call_args.kwargs["session_name"])

    def test_question_uses_same_session_title_as_notifications(self) -> None:
        plugin = load_plugin()
        adapter = plugin.HermesTelegramAdapter(ask=mock.Mock(return_value=[["예"]]))
        with mock.patch.object(adapter, "_session_name", return_value="제목 표시 구현") as resolve:
            result = json.loads(adapter.ask_user({"questions": [{"question": "진행?"}]}, session_id="s1"))
        self.assertEqual(result["answers"], [["예"]])
        resolve.assert_called_once_with("s1")
        self.assertEqual(adapter.ask.call_args.kwargs["session_name"], "제목 표시 구현")

    def test_session_title_missing_database_does_not_create_it(self) -> None:
        plugin = load_plugin()
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            constants = types.SimpleNamespace(get_hermes_home=lambda: home)
            with mock.patch.dict(sys.modules, {"hermes_constants": constants}):
                self.assertEqual(plugin.HermesTelegramAdapter._session_name("session-123"), "Hermes · session-")
                self.assertEqual(plugin.HermesTelegramAdapter._session_name(""), "Hermes")
            self.assertFalse((home / "state.db").exists())

    def test_session_title_empty_unknown_and_invalid_database_fall_back(self) -> None:
        plugin = load_plugin()
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            constants = types.SimpleNamespace(get_hermes_home=lambda: home)
            with closing(sqlite3.connect(home / "state.db")) as db, db:
                db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT)")
                db.executemany("INSERT INTO sessions VALUES (?, ?)", [("empty", " \n "), ("long", "가" * 120)])
            with mock.patch.dict(sys.modules, {"hermes_constants": constants}):
                self.assertEqual(plugin.HermesTelegramAdapter._session_name("empty"), "Hermes · empty")
                self.assertEqual(plugin.HermesTelegramAdapter._session_name("unknown"), "Hermes · unknown")
                self.assertEqual(plugin.HermesTelegramAdapter._session_name("long"), "가" * 100)
                (home / "state.db").write_text("not a database")
                self.assertEqual(plugin.HermesTelegramAdapter._session_name("broken"), "Hermes · broken")

    def test_adapter_loads_its_checkout_not_unrelated_common_module(self) -> None:
        plugin = load_plugin()
        with mock.patch.dict(sys.modules, {"common": object()}):
            self.assertTrue(callable(plugin._bridge_common().ask_questions))

    def test_question_round_trip_through_standalone_receiver(self) -> None:
        import tempfile
        import threading
        import time
        sys.path.insert(0, str(ROOT / "scripts"))
        import poller

        plugin = load_plugin()
        common = plugin._bridge_common()
        config = common.TelegramConfig("123:test-hermes-roundtrip", 42, (42,), Path("/unused"))
        result: list[dict] = []
        sent: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch.object(common, "load_telegram_config", return_value=config),
                mock.patch.object(common, "send_message", side_effect=lambda _cfg, text, **_kw: sent.append(text) or 10),
                mock.patch.object(common, "edit_message"),
                mock.patch.object(common, "mirror_terminal_event"),
                mock.patch.object(poller, "data_dir", return_value=Path(directory)),
            ):
                adapter = plugin.HermesTelegramAdapter(send=mock.Mock())
                worker = threading.Thread(target=lambda: result.append(json.loads(adapter.ask_user({
                    "questions": [{"question": "선택?", "choices": ["예", "아니오"]}],
                    "timeout_seconds": 5,
                }, task_id="test-session"))))
                worker.start()
                root = common.pending_question_dir(config)
                deadline = time.monotonic() + 4
                paths = []
                while time.monotonic() < deadline:
                    paths = list(root.glob("*.json"))
                    if paths:
                        break
                    time.sleep(0.02)
                self.assertEqual(len(paths), 1)
                callback = poller.CALLBACK_QUESTION_RE.match(f"q:{paths[0].stem}:0:0")
                self.assertIsNotNone(callback)
                receiver = poller.StandaloneTelegramPoller(config)
                with mock.patch.object(receiver, "_safe_edit"):
                    receiver._handle_question_callback(callback, 10, 42, 42)
                worker.join(timeout=5)
                self.assertFalse(worker.is_alive())
                self.assertEqual(result, [{"answers": [["예"]]}])
                self.assertIn("[Hermes]", sent[0])
                root.rmdir()

    def test_register_wires_native_tool_hooks_and_transport(self) -> None:
        plugin = load_plugin()
        ctx = mock.Mock()
        plugin.register(ctx)
        self.assertEqual(
            [args.args[0] for args in ctx.register_hook.call_args_list],
            ["post_llm_call", "on_session_end", "pre_llm_call"],
        )
        self.assertEqual(ctx.register_tool.call_args.kwargs["name"], "hermes_telegram_ask_user")
        self.assertEqual(ctx.register_approval_transport.call_args.args[0], "codex-telegram")


if __name__ == "__main__":
    unittest.main()
