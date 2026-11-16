from __future__ import annotations

import sys
import os
import json
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import common  # noqa: E402
import poller  # noqa: E402


def make_config(label: str) -> common.TelegramConfig:
    return common.TelegramConfig(
        f"123456:{label}-{time.time_ns()}",
        42,
        (42,),
        None,
    )


class StandalonePollerTests(unittest.TestCase):
    def make_poller(
        self,
        config: common.TelegramConfig,
        data_root: Path,
    ) -> poller.StandaloneTelegramPoller:
        with mock.patch.object(poller, "data_dir", return_value=data_root):
            return poller.StandaloneTelegramPoller(config)

    def test_empty_allowlist_is_never_authorized(self) -> None:
        config = common.TelegramConfig("token", 42, (), None)
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_poller(config, Path(directory))

        self.assertFalse(
            service._is_authorized(
                {"from": {"id": 42}, "message": {"chat": {"id": 42, "type": "private"}}}
            )
        )

    def test_safe_edit_masks_visible_secrets(self) -> None:
        config = make_config("safe-edit")
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_poller(config, Path(directory))
            with mock.patch.object(service, "api", return_value={}) as api:
                service._safe_edit(
                    7,
                    f"token={config.bot_token}",
                    {
                        "inline_keyboard": [
                            [
                                {
                                    "text": f"secret={config.bot_token}",
                                    "callback_data": "q:hash:0:0",
                                }
                            ]
                        ]
                    },
                )

        payload = api.call_args.args[1]
        self.assertNotIn(config.bot_token, payload["text"])
        self.assertNotIn(
            config.bot_token,
            payload["reply_markup"]["inline_keyboard"][0][0]["text"],
        )
        self.assertEqual(
            payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"],
            "q:hash:0:0",
        )

    def test_single_choice_question_hands_answer_to_codex_owner(self) -> None:
        config = make_config("question")
        with tempfile.TemporaryDirectory() as directory:
            data_root = Path(directory)
            service = self.make_poller(config, data_root)
            pending_dir = common.pending_question_dir(config)
            pending_dir.mkdir(parents=True, exist_ok=True)
            path = pending_dir / "short-hash.json"
            common.atomic_write_json(
                path,
                {
                    "requestID": "request",
                    "sessionID": "session",
                    "serverUrl": "http://127.0.0.1:43991/",
                    "questions": [
                        {
                            "header": "확인",
                            "question": "진행할까요?",
                            "options": [{"label": "예", "description": ""}],
                            "multiple": False,
                            "custom": True,
                        }
                    ],
                    "telegramMessageIds": [10],
                    "currentQuestionIndex": 0,
                    "answersInProgress": [None],
                    "expiresAt": int(time.time() * 1000) + 60_000,
                },
            )
            callback = poller.CALLBACK_QUESTION_RE.match("q:short-hash:0:0")
            assert callback is not None
            with mock.patch.object(service, "_safe_edit") as edit:
                service._handle_question_callback(callback, 10, 42, 42)
            payload = common.read_json(path)
            self.assertEqual(payload["answersInProgress"], [["예"]])
            self.assertIsInstance(payload["submittedAt"], int)
            edit.assert_called_once()
            path.unlink()
            pending_dir.rmdir()

    def test_hermes_question_status_retains_identity(self) -> None:
        config = make_config("hermes")
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_poller(config, Path(directory))
            path = Path(directory) / "pending.json"
            pending = {
                "agentName": "Hermes", "sessionTitle": "Hermes · 작업",
                "questions": [{"header": "확인", "question": "진행할까요?", "options": []}],
                "answersInProgress": [["예"]],
            }
            with mock.patch.object(service, "_safe_edit") as edit:
                service._complete_question_if_ready(path, "hash", pending, 10)
            self.assertIn("Hermes에 답변", edit.call_args.args[1])

    def test_permission_callback_reaches_one_shot_codex_server(self) -> None:
        config = make_config("permission")
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_poller(config, Path(directory))
            result: list[str] = []

            def request() -> None:
                with mock.patch.object(common, "send_message", return_value=11):
                    result.append(
                        common.request_permission(
                            session_id="thread",
                            title="echo ok",
                            permission="Bash",
                            patterns=["echo ok"],
                            directory="/tmp",
                            timeout_seconds=5,
                            config=config,
                        )
                    )

            thread = threading.Thread(target=request)
            thread.start()
            pending_dir = common.pending_permission_dir(config)
            deadline = time.monotonic() + 5
            path = None
            while time.monotonic() < deadline:
                files = list(pending_dir.glob("*.json"))
                if files:
                    path = files[0]
                    break
                time.sleep(0.02)
            self.assertIsNotNone(path)
            callback = poller.CALLBACK_PERMISSION_RE.match(f"p:{path.stem}:a")
            assert callback is not None
            with mock.patch.object(service, "_safe_edit"):
                service._handle_permission_callback(callback, 11)
            thread.join(timeout=5)
            self.assertEqual(result, ["always"])
            self.assertFalse(thread.is_alive())
            if pending_dir.exists():
                pending_dir.rmdir()

    def test_multiple_choice_toggles_then_submits_on_done(self) -> None:
        config = make_config("multiple")
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_poller(config, Path(directory))
            pending_dir = common.pending_question_dir(config)
            pending_dir.mkdir(parents=True, exist_ok=True)
            path = pending_dir / "multiple-hash.json"
            common.atomic_write_json(
                path,
                {
                    "requestID": "request",
                    "sessionID": "session",
                    "serverUrl": "http://127.0.0.1:43991/",
                    "questions": [
                        {
                            "header": "선택",
                            "question": "고르세요",
                            "options": [
                                {"label": "A", "description": ""},
                                {"label": "B", "description": ""},
                            ],
                            "multiple": True,
                            "custom": True,
                        }
                    ],
                    "telegramMessageIds": [10],
                    "currentQuestionIndex": 0,
                    "answersInProgress": [None],
                    "expiresAt": int(time.time() * 1000) + 60_000,
                },
            )
            select = poller.CALLBACK_QUESTION_RE.match("q:multiple-hash:0:0")
            done = poller.CALLBACK_QUESTION_RE.match("q:multiple-hash:0:d")
            assert select is not None and done is not None
            with mock.patch.object(service, "_safe_edit"):
                service._handle_question_callback(select, 10, 42, 42)
                selected = common.read_json(path)
                self.assertEqual(selected["answersInProgress"], [["A"]])
                self.assertNotIn("submittedAt", selected)
                service._handle_question_callback(done, 10, 42, 42)
            submitted = common.read_json(path)
            self.assertIsInstance(submitted["submittedAt"], int)
            path.unlink()
            pending_dir.rmdir()

    def test_custom_answer_is_correlated_by_force_reply_message(self) -> None:
        config = make_config("custom")
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_poller(config, Path(directory))
            pending_dir = common.pending_question_dir(config)
            pending_dir.mkdir(parents=True, exist_ok=True)
            path = pending_dir / "custom-hash.json"
            common.atomic_write_json(
                path,
                {
                    "requestID": "request",
                    "sessionID": "session",
                    "serverUrl": "http://127.0.0.1:43991/",
                    "questions": [
                        {
                            "header": "입력",
                            "question": "답변하세요",
                            "options": [],
                            "multiple": False,
                            "custom": True,
                        }
                    ],
                    "telegramMessageIds": [10],
                    "currentQuestionIndex": 0,
                    "answersInProgress": [None],
                    "expiresAt": int(time.time() * 1000) + 60_000,
                },
            )
            callback = poller.CALLBACK_QUESTION_RE.match("q:custom-hash:0:c")
            assert callback is not None
            with (
                mock.patch.object(service, "_safe_edit"),
                mock.patch.object(poller, "send_message", return_value=20),
            ):
                service._handle_question_callback(callback, 10, 42, 42)
                handled = service._handle_custom_answer("직접 답변", 42, 42, 20)
            self.assertTrue(handled)
            submitted = common.read_json(path)
            self.assertEqual(submitted["answersInProgress"], [["직접 답변"]])
            self.assertIsInstance(submitted["submittedAt"], int)
            path.unlink()
            pending_dir.rmdir()

    @unittest.skipUnless(os.name == "posix" and Path("/tmp").is_dir(), "POSIX shared temp path")
    def test_question_roundtrip_when_sender_has_a_different_tmpdir(self) -> None:
        config = make_config("different-tmpdir")
        child_code = '''
import json, sys
from unittest import mock
import common
config = common.TelegramConfig(sys.argv[1], 42, (42,), None)
with mock.patch.object(common, "send_message", return_value=10), \\
     mock.patch.object(common, "edit_message"), \\
     mock.patch.object(common, "mirror_terminal_event"):
    answers = common.ask_questions([{"question": "Choose", "options": ["Yes"]}],
        session_id="session", server_url="http://127.0.0.1:1", config=config, timeout_seconds=5)
    print(json.dumps(answers), flush=True)
'''
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_poller(config, Path(directory))
            env = dict(os.environ, TMPDIR=directory, PYTHONPATH=str(ROOT / "scripts"))
            child = subprocess.Popen([sys.executable, "-c", child_code, config.bot_token],
                                     env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            pending_dir = common.pending_question_dir(config)
            try:
                deadline = time.monotonic() + 5
                paths = []
                while time.monotonic() < deadline:
                    paths = list(pending_dir.glob("*.json"))
                    if paths:
                        break
                    if child.poll() is not None:
                        break
                    time.sleep(0.01)
                self.assertEqual(len(paths), 1, "sender did not create a shared pending question")
                match = poller.CALLBACK_QUESTION_RE.match(f"q:{paths[0].stem}:0:0")
                assert match is not None
                with mock.patch.object(service, "_safe_edit"):
                    service._handle_question_callback(match, 10, 42, 42)
                stdout, stderr = child.communicate(timeout=5)
                self.assertEqual(child.returncode, 0, stderr)
                self.assertEqual(json.loads(stdout), [["Yes"]])
            finally:
                if child.poll() is None:
                    child.kill()
                    child.communicate()
                for path in pending_dir.glob("*.json"):
                    path.unlink()
                if pending_dir.exists():
                    pending_dir.rmdir()

    def test_custom_answer_plain_message_and_concurrent_questions(self) -> None:
        config = make_config("custom-plain")
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_poller(config, Path(directory))
            pending_dir = common.pending_question_dir(config)
            pending_dir.mkdir(parents=True, exist_ok=True)
            paths = [pending_dir / "codex.json", pending_dir / "hermes.json"]
            for path, agent, prompt in zip(paths, ("Codex", "Hermes"), (20, 21)):
                common.atomic_write_json(path, {
                    "agentName": agent,
                    "questions": [{"question": "답변하세요", "options": [], "multiple": False}],
                    "answersInProgress": [None], "telegramMessageIds": [10],
                    "awaitingCustomFor": {"chatId": 42, "userId": 42, "promptMessageId": prompt, "questionIndex": 0},
                    "expiresAt": int(time.time() * 1000) + 60_000,
                })
            message = {"from": {"id": 42}, "chat": {"id": 42, "type": "private"}, "text": "직접 답변"}
            with mock.patch.object(service, "_safe_edit"), mock.patch.object(poller, "send_message") as send:
                service._handle_message(message)
                self.assertIn("여러 질문", send.call_args.args[1])
                self.assertEqual(common.read_json(paths[0])["answersInProgress"], [None])
                self.assertEqual(common.read_json(paths[1])["answersInProgress"], [None])
                message["reply_to_message"] = {"message_id": 21}
                service._handle_message(message)
                self.assertEqual(common.read_json(paths[1])["answersInProgress"], [["직접 답변"]])
                del message["reply_to_message"]
                message["text"] = "/help"
                service._handle_message(message)
                self.assertEqual(common.read_json(paths[0])["answersInProgress"], [None])
                message["text"] = "다른 답변"
                service._handle_message(message)
                self.assertEqual(common.read_json(paths[0])["answersInProgress"], [["다른 답변"]])
            for path in paths:
                path.unlink()
            pending_dir.rmdir()

    def test_sessions_command_saves_numbered_codex_snapshot(self) -> None:
        config = make_config("sessions")
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_poller(config, Path(directory))
            with (
                mock.patch.object(
                    service,
                    "_bridge_request",
                    side_effect=[
                        (
                            200,
                            [
                                {
                                    "id": "thread-1",
                                    "title": "테스트 세션",
                                    "parentID": None,
                                }
                            ],
                        ),
                        (200, {"thread-1": {"type": "idle"}}),
                    ],
                ),
                mock.patch.object(poller, "send_message", return_value=30) as send,
            ):
                service._send_sessions()
            snapshot = common.read_json(service._snapshot_path())
            self.assertEqual(snapshot["entries"][0]["sessionId"], "thread-1")
            self.assertEqual(snapshot["entries"][0]["index"], 1)
            self.assertIn("1. ⚪ 테스트 세션", send.call_args.args[1])

    def test_polling_lock_uses_the_codex_temporary_namespace(self) -> None:
        config = make_config("lock-namespace")
        lock = poller.PollingLock(config)
        self.assertEqual(
            lock.path,
            common.shared_temp_dir() / f"codex-telegram-{config.token_hash}.lock",
        )

    def test_token_scoped_lock_allows_only_one_owner(self) -> None:
        config = make_config("lock")
        first = poller.PollingLock(config)
        second = poller.PollingLock(config)
        self.assertTrue(first.acquire())
        try:
            self.assertFalse(second.acquire())
        finally:
            first.release()

    def test_unauthorized_private_message_is_ignored(self) -> None:
        config = make_config("auth")
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_poller(config, Path(directory))
            with mock.patch.object(service, "_send_help") as help_message:
                service._handle_message(
                    {
                        "chat": {"id": 99, "type": "private"},
                        "from": {"id": 99},
                        "text": "/help",
                    }
                )
            help_message.assert_not_called()


if __name__ == "__main__":
    unittest.main()
