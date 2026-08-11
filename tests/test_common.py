from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import common  # noqa: E402


def make_config(label: str) -> common.TelegramConfig:
    token = f"123456:{label}-{time.time_ns()}"
    return common.TelegramConfig(token, 42, (42,), Path("/tmp/test.env"))


class CommonTests(unittest.TestCase):
    def test_sensitive_text_redaction_masks_common_credentials(self) -> None:
        bot_token = "123456789:" + ("A" * 28)
        api_secret = "super-" + "secret-value"
        bearer = ".".join(("eyJhbGciOiJIUzI1NiJ9", "payload", "signature"))
        openai_token = "sk-" + "proj-" + ("A" * 24)
        url_secret = "url-" + "secret-value"
        original = (
            f"bot={bot_token}\n"
            f"api_key='{api_secret}'\n"
            f"Authorization: Bearer {bearer}\n"
            f"OpenAI {openai_token}\n"
            f"URL https://example.test/?token={url_secret}&safe=yes"
        )
        redacted = common.redact_sensitive_text(
            original,
            extra_secrets=(bot_token,),
        )
        self.assertNotIn(bot_token, redacted)
        self.assertNotIn(api_secret, redacted)
        self.assertNotIn(bearer, redacted)
        self.assertNotIn(openai_token, redacted)
        self.assertNotIn(url_secret, redacted)
        self.assertGreaterEqual(redacted.count(common.REDACTION_MARKER), 5)
        self.assertIn("&safe=yes", redacted)

    def test_sensitive_text_redaction_masks_local_identity(self) -> None:
        original = f"path={Path.home()}/project host={common.socket.gethostname()}"
        redacted = common.redact_sensitive_text(original)
        self.assertNotIn(str(Path.home()), redacted)
        self.assertNotIn(common.socket.gethostname(), redacted)
        self.assertIn("path=~/project", redacted)
        self.assertIn("host=[LOCAL_HOST]", redacted)

    def test_sensitive_text_redaction_preserves_regular_text(self) -> None:
        text = "세션 완료: 12 tests passed, token budget: none"
        self.assertEqual(common.redact_sensitive_text(text), text)

    def test_send_and_edit_mask_text_and_visible_button_labels(self) -> None:
        config = make_config("redaction-boundary")
        secret = config.bot_token
        markup = {
            "inline_keyboard": [
                [
                    {
                        "text": f"token={secret}",
                        "callback_data": f"keep:{secret}",
                    }
                ]
            ]
        }
        with mock.patch.object(
            common,
            "_telegram_api",
            side_effect=[{"message_id": 7}, {}],
        ) as telegram_api:
            message_id = common.send_message(
                config,
                f"secret={secret}",
                reply_markup=markup,
            )
            common.edit_message(config, message_id, f"password={secret}")

        send_payload = telegram_api.call_args_list[0].args[2]
        edit_payload = telegram_api.call_args_list[1].args[2]
        self.assertEqual(message_id, 7)
        self.assertNotIn(secret, send_payload["text"])
        self.assertNotIn(
            secret,
            send_payload["reply_markup"]["inline_keyboard"][0][0]["text"],
        )
        self.assertEqual(
            send_payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"],
            f"keep:{secret}",
        )
        self.assertNotIn(secret, edit_payload["text"])

    def test_terminal_mirror_writes_safe_correlated_event(self) -> None:
        stream = io.StringIO()
        with mock.patch.object(common, "terminal_mirror_enabled", return_value=True):
            written = common.mirror_terminal_event(
                "권한\x1b",
                "req-123",
                "승인 대기",
                ["명령: echo ok\n두 번째 줄"],
                state="Telegram 응답 대기",
                stream=stream,
            )
        output = stream.getvalue()
        self.assertTrue(written)
        self.assertIn("Codex Telegram · 권한? · req-123", output)
        self.assertIn("│ 명령: echo ok", output)
        self.assertIn("│ 두 번째 줄", output)
        self.assertNotIn("\x1b", output)

    def test_terminal_mirror_can_be_disabled_by_environment(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"CODEX_TELEGRAM_TERMINAL_MIRROR": "0"},
            clear=False,
        ):
            self.assertFalse(common.terminal_mirror_enabled())

    def test_hashes_match_opencode_shapes(self) -> None:
        self.assertEqual(
            common.question_short_hash("request", "session", "http://127.0.0.1:1/"),
            "RG-wFmrper",
        )
        self.assertEqual(
            common.permission_short_hash(
                "request",
                "session",
                "request",
                "http://127.0.0.1:1/",
            ),
            "0LyzvUXKIG",
        )

    def test_normalize_questions_accepts_options_and_custom(self) -> None:
        questions = common.normalize_questions(
            [
                {
                    "header": "범위",
                    "question": "어디까지 할까요?",
                    "options": [
                        {"label": "최소", "description": "필수만"},
                        "전체",
                    ],
                }
            ]
        )
        self.assertEqual(questions[0]["options"][0]["description"], "필수만")
        self.assertEqual(questions[0]["options"][1]["label"], "전체")
        self.assertTrue(questions[0]["custom"])

    def test_question_text_starts_with_session_title(self) -> None:
        text = common._question_text(
            [
                {
                    "header": "확인",
                    "question": "진행할까요?",
                    "options": [{"label": "예", "description": ""}],
                }
            ],
            0,
            session_name="Telegram 표시 점검",
        )

        self.assertEqual(text.splitlines()[0], "🧵 세션: Telegram 표시 점검")
        self.assertIn("❓ [Codex] 확인", text)

    def test_resolve_session_name_uses_local_bridge_title(self) -> None:
        response = io.BytesIO(
            json.dumps({"title": "  실제   세션 제목  "}).encode("utf-8")
        )
        with mock.patch.object(common.urllib.request, "urlopen", return_value=response):
            title = common.resolve_session_name(
                "thread-1",
                fallback="fallback",
                server_url="http://127.0.0.1:43991/",
            )

        self.assertEqual(title, "실제 세션 제목")

    def test_question_handoff_uses_shared_pending_file(self) -> None:
        config = make_config("question")
        pending_dir = common.pending_question_dir(config)
        pending_dir.mkdir(parents=True, exist_ok=True)

        def answer() -> None:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                files = list(pending_dir.glob("*.json"))
                if files:
                    payload = common.read_json(files[0], {})
                    payload["answersInProgress"] = [["예"]]
                    payload["submittedAt"] = int(time.time() * 1000)
                    common.atomic_write_json(files[0], payload)
                    return
                time.sleep(0.02)
            raise AssertionError("pending question was not created")

        responder = threading.Thread(target=answer)
        responder.start()
        sent_message: dict[str, str] = {}

        def capture_message(
            _config: common.TelegramConfig,
            text: str,
            **_kwargs: object,
        ) -> int:
            sent_message["text"] = text
            return 10

        with (
            mock.patch.object(common, "send_message", side_effect=capture_message),
            mock.patch.object(common, "edit_message") as edit,
            mock.patch.object(common, "mirror_terminal_event") as mirror,
        ):
            answers = common.ask_questions(
                [
                    {
                        "header": "확인",
                        "question": "진행할까요?",
                        "options": [{"label": "예", "description": ""}],
                    }
                ],
                session_id="thread-1",
                server_url="http://127.0.0.1:43991/",
                timeout_seconds=5,
                config=config,
                session_name="현재 세션 제목",
            )
        responder.join(timeout=2)
        self.assertEqual(answers, [["예"]])
        self.assertTrue(sent_message["text"].startswith("🧵 세션: 현재 세션 제목\n"))
        edit.assert_called_once()
        self.assertEqual(mirror.call_count, 2)
        self.assertEqual(mirror.call_args_list[0].args[1], mirror.call_args_list[1].args[1])
        self.assertEqual(list(pending_dir.glob("*.json")), [])
        pending_dir.rmdir()

    def test_question_timeout_expires_telegram_buttons(self) -> None:
        config = make_config("question-timeout")
        pending_dir = common.pending_question_dir(config)
        with (
            mock.patch.object(common, "send_message", return_value=12),
            mock.patch.object(common, "edit_message") as edit,
        ):
            with self.assertRaisesRegex(TimeoutError, "timed out"):
                common.ask_questions(
                    [
                        {
                            "header": "확인",
                            "question": "진행할까요?",
                            "options": [{"label": "예", "description": ""}],
                        }
                    ],
                    session_id="thread-timeout",
                    server_url="http://127.0.0.1:43991/",
                    timeout_seconds=0,
                    config=config,
                    session_name="만료 테스트",
                )
        edit.assert_called_once_with(
            config,
            12,
            "🧵 세션: 만료 테스트\n\n⌛ Codex 질문이 만료됐습니다.",
        )
        self.assertEqual(list(pending_dir.glob("*.json")), [])
        pending_dir.rmdir()

    def test_permission_callback_posts_to_one_shot_local_server(self) -> None:
        config = make_config("permission")
        pending_dir = common.pending_permission_dir(config)
        pending_dir.mkdir(parents=True, exist_ok=True)

        def approve() -> None:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                files = list(pending_dir.glob("*.json"))
                if files:
                    payload = common.read_json(files[0], {})
                    url = urllib.parse.urljoin(
                        payload["serverUrl"],
                        f"/permission/{urllib.parse.quote(payload['requestID'], safe='')}/reply",
                    )
                    request = urllib.request.Request(
                        url,
                        data=json.dumps({"reply": "always"}).encode(),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(request, timeout=2) as response:
                        self.assertEqual(response.status, 200)
                    return
                time.sleep(0.02)
            raise AssertionError("pending permission was not created")

        responder = threading.Thread(target=approve)
        responder.start()
        with (
            mock.patch.object(common, "send_message", return_value=11),
            mock.patch.object(common, "mirror_terminal_event") as mirror,
        ):
            reply = common.request_permission(
                session_id="thread-2",
                title="echo ok",
                permission="Bash",
                patterns=["echo ok"],
                directory="/tmp",
                timeout_seconds=5,
                config=config,
            )
        responder.join(timeout=2)
        self.assertEqual(reply, "always")
        self.assertEqual(mirror.call_count, 2)
        self.assertEqual(mirror.call_args_list[0].args[1], mirror.call_args_list[1].args[1])
        self.assertEqual(list(pending_dir.glob("*.json")), [])
        pending_dir.rmdir()

    def test_register_session_writes_opencode_registry_shape_and_modes(self) -> None:
        config = make_config("registry")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(common, "shared_config_dir", return_value=root):
                common.register_session(
                    "thread-registry",
                    title="Codex test",
                    cwd="/tmp",
                    status="busy",
                    config=config,
                )
                files = list(root.rglob("*.json"))
                self.assertEqual(len(files), 1)
                payload = json.loads(files[0].read_text())
                self.assertEqual(payload["version"], 1)
                self.assertEqual(payload["entry"]["agent"], "codex")
                self.assertEqual(payload["entry"]["status"], "busy")
                self.assertEqual(os.stat(files[0]).st_mode & 0o777, 0o600)
                self.assertEqual(os.stat(files[0].parent).st_mode & 0o777, 0o700)

    def test_environment_only_credentials_do_not_require_an_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.env"
            with mock.patch.dict(
                os.environ,
                {
                    "TELEGRAM_BOT_TOKEN": "123456:environment-only",
                    "TELEGRAM_CHAT_ID": "42",
                    "TELEGRAM_ALLOWED_USER_IDS": "42",
                },
                clear=False,
            ):
                config = common.load_telegram_config(missing)
        self.assertEqual(config.bot_token, "123456:environment-only")
        self.assertEqual(config.chat_id, 42)
        self.assertEqual(config.env_path, missing)

    def test_runtime_config_requires_an_explicit_allowlist(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "123456:environment-only",
                "TELEGRAM_CHAT_ID": "42",
                "TELEGRAM_ALLOWED_USER_IDS": "",
            },
            clear=False,
        ):
            with self.assertRaisesRegex(
                common.TelegramBridgeError,
                "must not be empty",
            ):
                common.load_telegram_config(Path("/missing/telegram.env"))

    def test_runtime_config_rejects_group_readable_credentials(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX mode validation")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "telegram.env"
            path.write_text(
                "\n".join(
                    (
                        "TELEGRAM_BOT_TOKEN=123456:test-runtime",
                        "TELEGRAM_CHAT_ID=42",
                        "TELEGRAM_ALLOWED_USER_IDS=42",
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            path.chmod(0o640)
            with self.assertRaisesRegex(
                common.TelegramBridgeError,
                "group/world accessible",
            ):
                common.load_telegram_config(path)

    def test_expired_pending_file_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pending.json"
            common.atomic_write_json(path, {"expiresAt": 0, "secret": "value"})
            self.assertIsNone(common.read_pending_json(path))
            self.assertFalse(path.exists())

    def test_pending_file_without_valid_expiry_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, payload in enumerate(({}, {"expiresAt": "later"}, [])):
                path = root / f"pending-{index}.json"
                common.atomic_write_json(path, payload)
                self.assertIsNone(common.read_pending_json(path))
                self.assertFalse(path.exists())

    def test_runtime_settings_select_standalone_shared_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "config.json"
            shared = root / "shared"
            common.atomic_write_json(
                settings,
                {
                    "mode": "standalone",
                    "env_path": str(root / ".env"),
                    "shared_dir": str(shared),
                },
            )
            with mock.patch.dict(
                os.environ,
                {"CODEX_TELEGRAM_CONFIG": str(settings)},
                clear=False,
            ):
                self.assertEqual(common.runtime_mode(), "standalone")
                self.assertEqual(common.shared_config_dir(), shared)


if __name__ == "__main__":
    unittest.main()
