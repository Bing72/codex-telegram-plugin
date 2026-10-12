from __future__ import annotations

import importlib.util
import json
import sys
import unittest
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
