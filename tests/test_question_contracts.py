from __future__ import annotations

import dataclasses
import json
import io
import os
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import bridge
import common
import mcp_server
import test_mcp_server
from test_hermes_plugin import load_plugin


class CodexQuestionContractTests(unittest.TestCase):
    def call(self, request_id=42):
        return dataclasses.replace(test_mcp_server.McpServerTests._call(),
                                   request_id=request_id, timeout_seconds=2, progress=None)

    def service(self):
        with mock.patch.object(bridge, "load_telegram_config", return_value=mock.Mock()):
            return bridge.CodexBridge()

    def test_rpc_request_ids_do_not_resolve_server_elicitation_requests(self):
        runtime = mcp_server.McpRuntime()
        waiter = runtime._register_outbound(7)
        messages = []
        with mock.patch.object(mcp_server, "write_message", side_effect=messages.append):
            runtime.dispatch({"jsonrpc": "2.0", "id": 7, "method": "ping"})
        self.assertTrue(waiter.empty())
        self.assertEqual(messages, [{"jsonrpc": "2.0", "id": 7, "result": {}}])
        response = {"jsonrpc": "2.0", "id": 7, "result": {"action": "cancel"}}
        runtime.dispatch(response)
        self.assertEqual(waiter.get_nowait(), response)

    def test_cancel_before_a_worker_starts_cannot_post_a_question(self):
        runtime = mcp_server.McpRuntime()
        messages = []
        with (mock.patch.object(mcp_server.threading, "Thread") as thread,
              mock.patch.object(mcp_server, "_telegram_answers") as telegram,
              mock.patch.object(mcp_server, "write_message", side_effect=messages.append)):
            runtime.dispatch({"id": 5, "method": "tools/call", "params": {"name": "ask_user"}})
            runtime.dispatch({"method": "notifications/cancelled", "params": {"requestId": 5}})
            thread.call_args.kwargs["target"](*thread.call_args.kwargs["args"])
        telegram.assert_not_called()
        self.assertEqual(messages, [])
        self.assertEqual(runtime._active_calls, {})

    def test_codex_question_count_matches_its_advertised_schema(self):
        with mock.patch.object(mcp_server, "resolve_session_name", return_value="test"):
            with self.assertRaisesRegex(ValueError, "three"):
                mcp_server._parse_ask_user_call({"id": 1, "params": {"name": "ask_user", "arguments": {
                    "questions": [{"header": "q", "question": "q"} for _ in range(4)],
                }}})

    def test_question_timeout_matches_its_advertised_bounds(self):
        for timeout in (-1, 0, 1, 86401, None, True, "30", 30.5):
            with self.subTest(timeout=timeout), self.assertRaisesRegex(ValueError, "timeout_seconds"):
                mcp_server._parse_ask_user_call({"id": 1, "params": {"name": "ask_user", "arguments": {
                    "timeout_seconds": timeout, "questions": [{"header": "q", "question": "q"}],
                }}})

    def test_native_free_text_rejects_missing_blank_or_non_text_values(self):
        form = mcp_server._elicitation_form(common.normalize_questions([
            {"header": "입력", "question": "설명"},
        ]), "session")
        for value in (None, "", "  ", 1, True, [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                mcp_server._decode_elicitation_answers(
                    {"result": {"action": "accept", "content": {"question_1": value}}}, form)

    def test_multi_select_custom_input_can_be_combined_with_known_choices(self):
        form = mcp_server._elicitation_form(common.normalize_questions([
            {"header": "선택", "question": "항목", "options": ["A", "B"], "multiple": True},
        ]), "session")
        custom = form.questions[0].custom_choice
        response = {"result": {"action": "accept", "content": {
            "question_1": ["A", custom], "question_1_custom": "추가 항목",
        }}}
        self.assertEqual(mcp_server._decode_elicitation_answers(response, form), [["A", "추가 항목"]])
        response["result"]["content"]["question_1"] = ["unknown", custom]
        with self.assertRaises(ValueError):
            mcp_server._decode_elicitation_answers(response, form)

    def test_explicit_empty_multi_selection_is_distinct_from_a_missing_answer(self):
        form = mcp_server._elicitation_form(common.normalize_questions([
            {"header": "선택", "question": "필요한 항목", "options": ["A", "B"], "multiple": True},
        ]), "session")
        self.assertEqual(form.params["requestedSchema"]["properties"]["question_1"]["minItems"], 0)
        self.assertEqual(mcp_server._decode_elicitation_answers(
            {"result": {"action": "accept", "content": {"question_1": []}}}, form), [[]])
        with self.assertRaises(ValueError):
            mcp_server._decode_elicitation_answers({"result": {"action": "accept", "content": {}}}, form)

    def test_native_cancel_stops_telegram_wait(self):
        for action in ("cancel",):
            with self.subTest(action=action):
                runtime = mcp_server.McpRuntime()
                cancelled = threading.Event()

                def telegram(_call, cancel_event=None):
                    if cancel_event is not None and cancel_event.wait(1):
                        cancelled.set()
                    return None

                with (mock.patch.object(mcp_server, "_telegram_answers", side_effect=telegram),
                      mock.patch.object(runtime, "_request_elicitation", return_value={"result": {"action": action}})):
                    started = time.monotonic()
                    with self.assertRaisesRegex(RuntimeError, "cancel"):
                        runtime._answer_via_both_channels(self.call())
                self.assertLess(time.monotonic() - started, 1)
                self.assertTrue(cancelled.wait(1))

    def test_declined_native_form_keeps_telegram_fallback_available(self):
        runtime = mcp_server.McpRuntime()

        def telegram(_call, cancel_event=None):
            time.sleep(0.05)
            return [["Telegram"]]

        with (mock.patch.object(mcp_server, "_telegram_answers", side_effect=telegram),
              mock.patch.object(runtime, "_request_elicitation", return_value={"result": {"action": "decline"}})):
            self.assertEqual(runtime._answer_via_both_channels(self.call()), ("telegram", [["Telegram"]]))

    def test_client_cancel_stops_pending_calls_with_and_without_native_elicitation(self):
        for native in (False, True):
            with self.subTest(native=native):
                runtime = mcp_server.McpRuntime()
                runtime.supports_elicitation = native
                started, stopped, native_stopped = threading.Event(), threading.Event(), threading.Event()
                messages = []

                def telegram(_call, cancel_event=None):
                    started.set()
                    if cancel_event is not None and cancel_event.wait(1):
                        stopped.set()
                    return None

                def elicit(_form, **kwargs):
                    kwargs["cancel_event"].wait(1)
                    native_stopped.set()
                    raise mcp_server.ElicitationCancelled()

                def write(message):
                    messages.append(message)

                with (mock.patch.object(mcp_server, "_parse_ask_user_call", return_value=self.call()),
                      mock.patch.object(mcp_server, "_telegram_answers", side_effect=telegram),
                      mock.patch.object(runtime, "_request_elicitation", side_effect=elicit),
                      mock.patch.object(mcp_server, "write_message", side_effect=write)):
                    runtime.dispatch({"jsonrpc": "2.0", "id": 42, "method": "tools/call",
                                      "params": {"name": "ask_user"}})
                    self.assertTrue(started.wait(1))
                    runtime.dispatch({"jsonrpc": "2.0", "method": "notifications/cancelled",
                                      "params": {"requestId": 42}})
                    deadline = time.monotonic() + 2
                    while 42 in runtime._active_calls and time.monotonic() < deadline:
                        time.sleep(0.005)
                    self.assertNotIn(42, runtime._active_calls)
                    self.assertTrue(stopped.wait(1))
                    if native:
                        self.assertTrue(native_stopped.wait(1))
                self.assertFalse(any(message.get("id") == 42 for message in messages))
                self.assertEqual(runtime._active_calls, {})

    def test_one_client_cancellation_does_not_cancel_another_question(self):
        runtime = mcp_server.McpRuntime()
        starts = {key: threading.Event() for key in (10, 20)}
        answers = {key: threading.Event() for key in (10, 20)}
        cancels = {}
        release_second = threading.Event()
        messages = []

        def telegram(call, cancel_event=None):
            key = call.request_id
            cancels[key] = cancel_event
            starts[key].set()
            if key == 10:
                cancel_event.wait(1)
                return None
            release_second.wait(1)
            return [["second answer"]]

        def write(message):
            messages.append(message)
            if message.get("id") in answers:
                answers[message["id"]].set()

        with (mock.patch.object(mcp_server, "_parse_ask_user_call", side_effect=lambda r: self.call(r["id"])),
              mock.patch.object(mcp_server, "_telegram_answers", side_effect=telegram),
              mock.patch.object(mcp_server, "write_message", side_effect=write)):
            for key in starts:
                runtime.dispatch({"jsonrpc": "2.0", "id": key, "method": "tools/call",
                                  "params": {"name": "ask_user"}})
            self.assertTrue(all(event.wait(1) for event in starts.values()))
            runtime.dispatch({"method": "notifications/cancelled", "params": {"requestId": 10}})
            deadline = time.monotonic() + 1
            while 10 in runtime._active_calls and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertNotIn(10, runtime._active_calls)
            self.assertFalse(cancels[20].is_set())
            release_second.set()
            self.assertTrue(answers[20].wait(1))
        second = next(message for message in messages if message.get("id") == 20)
        self.assertFalse(any(message.get("id") == 10 for message in messages))
        self.assertEqual(second["result"]["structuredContent"], {"answers": [["second answer"]]})

    def test_native_user_cancel_is_reported_to_a_client_that_is_still_waiting(self):
        runtime = mcp_server.McpRuntime()
        runtime.supports_elicitation = True
        stopped = threading.Event()
        messages = []

        def telegram(_call, cancel_event=None):
            if cancel_event.wait(1):
                stopped.set()
            return None

        with (mock.patch.object(mcp_server, "_parse_ask_user_call", return_value=self.call()),
              mock.patch.object(mcp_server, "_telegram_answers", side_effect=telegram),
              mock.patch.object(runtime, "_request_elicitation", return_value={"result": {"action": "cancel"}}),
              mock.patch.object(mcp_server, "write_message", side_effect=messages.append)):
            runtime._handle_ask_user({"id": 42})
            self.assertTrue(stopped.wait(1))
        self.assertEqual(messages[-1]["error"]["code"], -32800)

    def test_secret_question_is_never_forwarded_to_telegram(self):
        service = self.service()
        with mock.patch.object(bridge, "ask_questions") as ask:
            with self.assertRaisesRegex(bridge.RpcError, "Secret input"):
                service.handle_server_request("item/tool/requestUserInput", {
                    "threadId": "thread", "questions": [{"id": "secret", "question": "secret prompt", "isSecret": True}],
                })
        ask.assert_not_called()

    def test_bridge_preserves_other_input_capability(self):
        service = self.service()
        for options, other, expected in (([{'label': 'A'}], False, False),
                                         ([{'label': 'A'}], True, True), (None, False, True)):
            with self.subTest(other=other, options=options):
                with mock.patch.object(bridge, "ask_questions", return_value=[["A"]]) as ask:
                    service.handle_server_request("item/tool/requestUserInput", {
                        "questions": [{"id": "q", "question": "input", "options": options, "isOther": other}],
                    })
                self.assertEqual(ask.call_args.args[0][0]["custom"], expected)

    def test_approval_never_grants_a_scope_the_host_did_not_offer(self):
        service = self.service()
        for reply, available, expected in (("always", ["accept", "decline"], "decline"),
                                            ("always", ["acceptForSession", "decline"], "acceptForSession"),
                                            ("once", ["decline", "cancel"], "decline"),
                                            ("unexpected", None, "decline")):
            with self.subTest(reply=reply, available=available):
                with mock.patch.object(bridge, "request_permission", return_value=reply) as ask:
                    result = service.handle_server_request("item/commandExecution/requestApproval", {
                        "availableDecisions": available, "command": "echo test",
                    })
                self.assertEqual(result, {"decision": expected})
                if available is not None:
                    self.assertEqual(ask.call_args.kwargs["allow_always"], "acceptForSession" in available)
                    self.assertEqual(ask.call_args.kwargs["allow_once"], "accept" in available)
        with mock.patch.object(bridge, "request_permission") as ask:
            with self.assertRaises(bridge.RpcError):
                service.handle_server_request("item/commandExecution/requestApproval", {"availableDecisions": []})
        ask.assert_not_called()

    def test_network_approval_identifies_its_destination(self):
        service = self.service()
        with mock.patch.object(bridge, "request_permission", return_value="reject") as ask:
            service.handle_server_request("item/commandExecution/requestApproval", {
                "networkApprovalContext": {"protocol": "https", "host": "example.test"},
            })
        self.assertEqual(ask.call_args.kwargs["permission"], "network")
        self.assertIn("example.test", ask.call_args.kwargs["title"])
        self.assertIn("https", ask.call_args.kwargs["title"])

    def test_blank_or_duplicate_labels_cannot_create_ambiguous_native_forms(self):
        for options in ([" "], ["A", " A "]):
            with self.subTest(options=options), self.assertRaises(common.TelegramBridgeError):
                common.normalize_questions([{"question": "pick", "options": options}])


class AppServerQuestionCancellationTests(unittest.TestCase):
    def client(self, handler):
        client = bridge.AppServerClient(handler, lambda *a: None)
        process = mock.Mock()
        process.stdin = io.StringIO()
        process.poll.return_value = None
        client.process = process
        return client, process

    def test_resolved_or_disconnected_request_cancels_wait_without_a_late_reply(self):
        for resolved in (True, False):
            with self.subTest(resolved=resolved):
                started, stopped = threading.Event(), threading.Event()

                def handler(_method, _params, *, cancel_event):
                    started.set()
                    self.assertTrue(cancel_event.wait(1))
                    stopped.set()
                    return {"answers": {}}

                client, process = self.client(handler)

                def lines():
                    yield json.dumps({"jsonrpc": "2.0", "id": 7, "method": "item/tool/requestUserInput", "params": {}})
                    self.assertTrue(started.wait(1))
                    if resolved:
                        yield json.dumps({"method": "serverRequest/resolved", "params": {"requestId": 7}})
                        self.assertTrue(stopped.wait(1))

                process.stdout = lines()
                with mock.patch.object(client, "respond") as respond:
                    client._read_loop()
                    self.assertTrue(stopped.wait(1))
                respond.assert_not_called()
                self.assertEqual(client._server_requests, {})

    def test_resolution_before_worker_start_does_not_post_a_question(self):
        handler = mock.Mock()
        client, process = self.client(handler)
        process.stdout = iter([
            json.dumps({"id": 8, "method": "item/tool/requestUserInput", "params": {}}),
            json.dumps({"method": "serverRequest/resolved", "params": {"requestId": 8}}),
        ])
        with mock.patch.object(bridge.threading, "Thread") as thread:
            client._read_loop()
        target = thread.call_args.kwargs["target"]
        target(*thread.call_args.kwargs["args"])
        handler.assert_not_called()

    def test_old_connection_cannot_reply_to_a_new_process(self):
        client, current = self.client(lambda *a, **kw: {"decision": "accept"})
        previous = mock.Mock()
        with mock.patch.object(bridge.LOG, "error"):
            client._handle_server_request({"id": 9, "method": "approval", "params": {}},
                                          threading.Event(), previous)
        self.assertEqual(current.stdin.getvalue(), "")

    def test_cancelled_permission_wait_is_retired_and_never_granted(self):
        config = common.TelegramConfig("123456:local-question-test", 42, (42,), None)
        cancel, sent = threading.Event(), threading.Event()
        failures = []

        def wait():
            try:
                common.request_permission(session_id="test", title="test", permission="Bash", patterns=[],
                                          directory="/tmp", config=config, cancel_event=cancel, timeout_seconds=2,
                                          allow_always=False)
            except common.TelegramBridgeError as exc:
                failures.append(str(exc))

        with (tempfile.TemporaryDirectory() as directory,
              mock.patch.object(common, "pending_permission_dir", return_value=Path(directory)/"permissions"),
              mock.patch.object(common, "send_message", side_effect=lambda *a, **kw: sent.set() or 10) as send,
              mock.patch.object(common, "edit_message") as edit,
              mock.patch.object(common, "mirror_terminal_event")):
            worker = threading.Thread(target=wait)
            worker.start()
            self.assertTrue(sent.wait(1))
            cancel.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(failures, ["Permission request cancelled"])
            self.assertFalse(list((Path(directory)/"permissions").glob("*.json")))
            edit.assert_called_once()
            buttons = send.call_args.kwargs["reply_markup"]["inline_keyboard"]
            self.assertFalse(any(button["callback_data"].endswith(":a") for row in buttons for button in row))

    def test_disallowed_permission_is_rejected_by_reply_server(self):
        server = common._PermissionHTTPServer("test-request", allow_always=False, allow_once=False)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            host, port = server.server_address
            for reply in ("always", "once"):
                request = urllib.request.Request(f"http://{host}:{port}/permission/test-request/reply",
                                                 data=json.dumps({"reply": reply}).encode(),
                                                 headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(request, timeout=2) as response:
                    self.assertEqual(response.status, 200)
                self.assertEqual(server.replies.get(timeout=1), "reject")
        finally:
            server.shutdown()
            server.server_close()
            worker.join(1)


class HermesQuestionContractTests(unittest.TestCase):
    def test_explicit_legacy_headless_question_uses_telegram_without_a_modal(self):
        plugin = load_plugin()
        adapter = plugin.HermesTelegramAdapter(ask=mock.Mock(return_value=[["Telegram"]]))
        terminal = types.SimpleNamespace(session_id="owner", _single_query_mode=True,
                                         _clarify_callback=mock.Mock())
        with (mock.patch.object(adapter, "_terminal", return_value=terminal),
              mock.patch.object(adapter, "_gateway_viewer", return_value=None)):
            result = json.loads(adapter.ask_user({"questions": [{"question": "입력?"}]}, session_id="owner"))
        self.assertEqual(result, {"answers": [["Telegram"]]})
        terminal._clarify_callback.assert_not_called()

    def test_legacy_child_cannot_borrow_the_parent_terminal(self):
        plugin = load_plugin()
        adapter = plugin.HermesTelegramAdapter(ask=mock.Mock(return_value=[["Telegram"]]))
        terminal = types.SimpleNamespace(session_id="parent", agent=types.SimpleNamespace(session_id="parent"),
                                         _clarify_callback=mock.Mock(), _single_query_mode=False)
        with (mock.patch.object(adapter, "_terminal", return_value=terminal),
              mock.patch.object(adapter, "_gateway_viewer", return_value=None)):
            result = json.loads(adapter.ask_user({"questions": [{"question": "입력?"}]}, session_id="child"))
            self.assertEqual(result, {"answers": [["Telegram"]]})
            with mock.patch.object(adapter, "ask_user") as mirror:
                native = mock.Mock(return_value="host policy")
                self.assertEqual(adapter.mirror_clarify(tool_name="clarify", args={}, next_call=native,
                                                       session_id="child"), "host policy")
                mirror.assert_not_called()
        terminal._clarify_callback.assert_not_called()

    def test_legacy_compression_keeps_the_current_agents_terminal(self):
        plugin = load_plugin()
        adapter = plugin.HermesTelegramAdapter()
        terminal = types.SimpleNamespace(session_id="old", agent=types.SimpleNamespace(session_id="compacted"))
        with (mock.patch.object(adapter, "_terminal", return_value=terminal),
              mock.patch.object(adapter, "_ask_with_terminal", return_value=[["native"]]) as ask):
            result = json.loads(adapter.ask_user({"questions": [{"question": "입력?"}]}, session_id="compacted"))
        self.assertEqual(result, {"answers": [["native"]]})
        self.assertEqual(ask.call_args.args[3]["session_id"], "compacted")

    def test_legacy_owner_supplies_the_session_id_when_host_context_is_missing(self):
        plugin = load_plugin()
        adapter = plugin.HermesTelegramAdapter()
        terminal = types.SimpleNamespace(session_id="owner", agent=None)
        with (mock.patch.object(adapter, "_terminal", return_value=terminal),
              mock.patch.object(adapter, "_ask_with_terminal", return_value=[["native"]]) as ask):
            adapter.ask_user({"questions": [{"question": "입력?"}]}, task_id="task")
        self.assertEqual(ask.call_args.args[3]["session_id"], "owner")

    def test_gateway_reload_cannot_reuse_stale_shared_question_helpers(self):
        plugin = load_plugin()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"scripts").mkdir()
            source = root/"scripts/common.py"
            source.write_text("version = 1\n")
            stamp = source.stat()
            installed = root/"adapter"
            installed.mkdir()
            (installed/"bridge-source.json").write_text(json.dumps({"plugin_root": str(root)}))
            with mock.patch.object(plugin, "__file__", str(installed/"__init__.py")):
                first = plugin._bridge_common()
                source.write_text("version = 2\n")
                os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
                second = plugin._bridge_common()
                self.assertIs(plugin._bridge_common(), second)
            self.assertEqual((first.version, second.version), (1, 2))
            self.assertIsNot(first, second)
            sys.modules.pop(first.__name__, None)
            sys.modules.pop(second.__name__, None)

    def test_five_questions_and_open_ended_multi_flag_preserve_input(self):
        plugin = load_plugin()
        questions = [{"question": f"질문 {i}", "choices": None, "multi_select": True} for i in range(5)]
        original = json.loads(json.dumps(questions))
        ask = mock.Mock(return_value=[[str(i)] for i in range(5)])
        adapter = plugin.HermesTelegramAdapter(ask=ask)
        with (mock.patch.object(adapter, "_terminal", return_value=None),
              mock.patch.object(adapter, "_gateway_viewer", return_value=None)):
            result = json.loads(adapter.ask_user({"questions": questions}))
        self.assertEqual(result, {"answers": [[str(i)] for i in range(5)]})
        self.assertEqual(questions, original)
        self.assertTrue(all(not question["multiple"] for question in ask.call_args.args[0]))

    def test_native_clarify_is_mirrored_with_its_original_result_contract(self):
        try:
            from hermes_cli import middleware, plugins
        except ImportError:
            self.skipTest("Run in the Hermes environment for native clarify integration")
        plugin = load_plugin()
        context = mock.Mock()
        plugin.register(context)
        adapter = context.register_tool.call_args.kwargs["handler"].__self__
        raw = {"questions": [{"question": "선택?", "choices": ["A", "B"]},
                             {"question": "복수?", "choices": ["A", "B"], "multi_select": True},
                             {"question": "설명?"}]}
        kind, mirror = context.register_middleware.call_args.args
        manager = types.SimpleNamespace(_middleware={kind: [mirror]},
                                        _report_hook_failure=mock.Mock())
        native = mock.Mock(return_value="native fallback")
        with (tempfile.TemporaryDirectory() as directory,
              mock.patch.dict(os.environ, {"HERMES_HOME": directory}),
              mock.patch.object(plugins, "_delivery_manager", return_value=manager),
              mock.patch("hermes_cli.lifecycle.invoke_hook"),
              mock.patch.object(adapter, "_terminal", return_value=types.SimpleNamespace(_single_query_mode=False)),
              mock.patch.object(adapter, "ask_user", return_value=json.dumps({
                  "answers": [["A (Recommended)"], ["A", "B"], ["설명"]],
              })) as mirrored):
            result = json.loads(middleware.run_tool_execution_middleware("clarify", raw, native,
                                                                         session_id="session"))
        native.assert_not_called()
        self.assertEqual(mirrored.call_args.kwargs["session_id"], "session")
        self.assertEqual(result["outcome"], "submitted")
        self.assertEqual([row["user_response"] for row in result["responses"]], ["A", ["A", "B"], "설명"])

    def test_native_clarify_cancel_fallback_and_privileged_inputs(self):
        try:
            import tools.clarify_tool
        except ImportError:
            self.skipTest("Run in the Hermes environment for native clarify integration")
        plugin = load_plugin()
        adapter = plugin.HermesTelegramAdapter()
        native = mock.Mock(return_value="native fallback")
        args = {"questions": [{"question": "입력?"}]}
        with (mock.patch("hermes_cli.lifecycle.invoke_hook"),
              mock.patch.object(adapter, "_terminal", return_value=types.SimpleNamespace(_single_query_mode=False))):
            with mock.patch.object(adapter, "ask_user", return_value=json.dumps({
                "answers": [[]], "outcome": "cancelled",
            })):
                result = json.loads(adapter.mirror_clarify(tool_name="clarify", args=args, next_call=native))
            self.assertEqual(result["outcome"], "cancelled")
            native.assert_not_called()
            with mock.patch.object(adapter, "ask_user", return_value=json.dumps({"error": "unavailable"})):
                self.assertEqual(adapter.mirror_clarify(tool_name="clarify", args=args, next_call=native), "native fallback")
            native.assert_called_once_with(args)
        for name in ("terminal", "sudo", "secret", "vault.unlock"):
            denied = mock.Mock(return_value="host denied")
            with mock.patch.object(adapter, "ask_user") as mirror:
                self.assertEqual(adapter.mirror_clarify(tool_name=name, args={}, next_call=denied), "host denied")
            mirror.assert_not_called()

    def test_native_headless_or_unowned_clarify_stays_on_its_host_path(self):
        plugin = load_plugin()
        adapter = plugin.HermesTelegramAdapter()
        for terminal in (None, types.SimpleNamespace(_single_query_mode=True)):
            with self.subTest(terminal=terminal):
                native = mock.Mock(return_value="host no-input policy")
                with (mock.patch.object(adapter, "_terminal", return_value=terminal),
                      mock.patch.object(adapter, "_gateway_viewer", return_value=None),
                      mock.patch.object(adapter, "ask_user") as mirror):
                    self.assertEqual(adapter.mirror_clarify(tool_name="clarify", args={}, next_call=native),
                                     "host no-input policy")
                native.assert_called_once_with({})
                mirror.assert_not_called()


if __name__ == "__main__":
    unittest.main()
