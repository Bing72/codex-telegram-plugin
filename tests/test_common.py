from __future__ import annotations

import html.parser
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


class TelegramHTMLProbe(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.text_parts: list[str] = []
        self.forbidden_code_parents: list[tuple[str, ...]] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        del attrs
        if tag == "code":
            forbidden_parents = tuple(
                parent for parent in self.stack if parent != "pre"
            )
            if forbidden_parents:
                self.forbidden_code_parents.append(forbidden_parents)
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack[-1] != tag:
            raise AssertionError(f"unbalanced Telegram HTML tag: {tag}")
        self.stack.pop()

    def handle_data(self, data: str) -> None:
        self.text_parts.append(data)

    @property
    def visible_text(self) -> str:
        return "".join(self.text_parts)


def probe_telegram_html(value: str) -> TelegramHTMLProbe:
    probe = TelegramHTMLProbe()
    probe.feed(value)
    probe.close()
    if probe.stack:
        raise AssertionError(f"unclosed Telegram HTML tags: {probe.stack}")
    return probe


def utf16_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def make_config(label: str) -> common.TelegramConfig:
    token = f"123456:{label}-{time.time_ns()}"
    return common.TelegramConfig(token, 42, (42,), Path("/tmp/test.env"))


class CommonTests(unittest.TestCase):
    def test_codex_markdown_formats_headings_and_lists_for_telegram(self) -> None:
        formatted = common.format_codex_markdown_for_telegram(
            "# 결과\n\n- 첫째\n- 둘째"
        )

        self.assertIn("<b>결과</b>", formatted)
        visible = probe_telegram_html(formatted).visible_text
        self.assertIn("• 첫째", visible)
        self.assertIn("• 둘째", visible)

    def test_codex_markdown_heading_preserves_a_literal_trailing_hash(self) -> None:
        formatted = common.format_codex_markdown_for_telegram("# C#")

        self.assertEqual(formatted, "<b>C#</b>")

    def test_codex_markdown_formats_bold_text_for_telegram(self) -> None:
        formatted = common.format_codex_markdown_for_telegram("**중요**")

        self.assertEqual(formatted, "<b>중요</b>")

    def test_codex_markdown_formats_italic_text_for_telegram(self) -> None:
        formatted = common.format_codex_markdown_for_telegram("*강조*")

        self.assertEqual(formatted, "<i>강조</i>")

    def test_codex_markdown_formats_strikethrough_text_for_telegram(self) -> None:
        formatted = common.format_codex_markdown_for_telegram("~~취소~~")

        self.assertEqual(formatted, "<s>취소</s>")

    def test_codex_markdown_formats_inline_code_without_interpreting_markup(self) -> None:
        formatted = common.format_codex_markdown_for_telegram("`a < b && c > d`")

        self.assertEqual(formatted, "<code>a &lt; b &amp;&amp; c &gt; d</code>")

    def test_inline_code_is_not_nested_inside_telegram_formatting_tags(self) -> None:
        cases = (
            ("heading", "# 제목 `x`", "제목 x"),
            ("bold", "**굵게 `x`**", "굵게 x"),
            ("italic", "*기울임 `x`*", "기울임 x"),
            ("strike", "~~취소 `x`~~", "취소 x"),
            ("link", "[문서 `x`](https://example.test)", "문서 x"),
            ("blockquote", "> 인용 `x`", "인용 x"),
        )
        for name, source, expected_visible in cases:
            with self.subTest(name=name):
                formatted = common.format_codex_markdown_for_telegram(source)
                probe = probe_telegram_html(formatted)

                self.assertEqual(probe.visible_text, expected_visible)
                self.assertEqual(probe.forbidden_code_parents, [])

    def test_codex_markdown_formats_fenced_code_as_preformatted_text(self) -> None:
        formatted = common.format_codex_markdown_for_telegram(
            "```python\nprint('<ok>')\n```"
        )

        self.assertIn("<pre>", formatted)
        self.assertIn("<code", formatted)
        self.assertIn("print('&lt;ok&gt;')", formatted)
        self.assertEqual(probe_telegram_html(formatted).visible_text, "print('<ok>')")

    def test_codex_markdown_formats_http_and_https_links_for_telegram(self) -> None:
        for target in (
            "http://example.test/plain",
            "https://example.test/a?q=1&lang=ko",
        ):
            with self.subTest(target=target):
                formatted = common.format_codex_markdown_for_telegram(
                    f"[문서]({target})"
                )
                escaped_target = target.replace("&", "&amp;")

                self.assertIn(
                    f'<a href="{escaped_target}">문서</a>',
                    formatted,
                )

    def test_codex_markdown_preserves_balanced_parentheses_in_link_target(self) -> None:
        target = "https://example.test/a_(b)/c?q=(x)"

        formatted = common.format_codex_markdown_for_telegram(
            f"[문서]({target})"
        )

        self.assertEqual(formatted, f'<a href="{target}">문서</a>')

    def test_codex_markdown_does_not_create_links_for_unsafe_schemes(self) -> None:
        formatted = common.format_codex_markdown_for_telegram(
            "[실행](javascript:alert(1))"
        )

        self.assertNotIn("<a ", formatted)
        self.assertIn(
            "javascript:alert(1)",
            probe_telegram_html(formatted).visible_text,
        )

    def test_codex_markdown_escapes_raw_html(self) -> None:
        formatted = common.format_codex_markdown_for_telegram(
            '<script>alert("x")</script> & done'
        )

        self.assertNotIn("<script>", formatted)
        self.assertIn("&lt;script&gt;", formatted)
        self.assertEqual(
            probe_telegram_html(formatted).visible_text,
            '<script>alert("x")</script> & done',
        )

    def test_codex_markdown_honors_backslash_escaped_syntax(self) -> None:
        source = (
            r"\# 제목" "\n"
            r"\*\*굵게\*\* \*기울임\* \~\~취소\~\~" "\n"
            r"\[문서\]\(https://example.test\)"
        )
        expected_visible = (
            "# 제목\n"
            "**굵게** *기울임* ~~취소~~\n"
            "[문서](https://example.test)"
        )

        formatted = common.format_codex_markdown_for_telegram(source)

        probe = probe_telegram_html(formatted)
        self.assertEqual(probe.visible_text, expected_visible)
        self.assertNotIn("<b>", formatted)
        self.assertNotIn("<i>", formatted)
        self.assertNotIn("<s>", formatted)
        self.assertNotIn("<a ", formatted)

    def test_codex_markdown_preserves_tables_as_preformatted_text(self) -> None:
        source = "| 이름 | 값 |\n| --- | --- |\n| a < b | 1 & 2 |"
        formatted = common.format_codex_markdown_for_telegram(source)

        self.assertIn("<pre>", formatted)
        self.assertEqual(probe_telegram_html(formatted).visible_text, source)

    def test_codex_markdown_conversion_does_not_mutate_the_tui_source(self) -> None:
        source = "# TUI 제목\n\n**Markdown** 원문"
        original = source[:]

        common.format_codex_markdown_for_telegram(source)

        self.assertEqual(source, original)

    def test_codex_markdown_conversion_does_not_make_network_requests(self) -> None:
        with mock.patch.object(common.urllib.request, "urlopen") as urlopen:
            common.format_codex_markdown_for_telegram("# 로컬 변환\n\n**완료**")

        urlopen.assert_not_called()

    def test_telegram_html_split_preserves_unicode_and_emoji(self) -> None:
        source = "한글🙂🌕abc" * 8
        formatted = common.format_codex_markdown_for_telegram(source)

        chunks = common.split_telegram_html(formatted, max_visible_units=12)

        probes = [probe_telegram_html(chunk) for chunk in chunks]
        self.assertGreater(len(chunks), 1)
        self.assertTrue(
            all(utf16_units(probe.visible_text) <= 12 for probe in probes)
        )
        self.assertEqual("".join(probe.visible_text for probe in probes), source)

    def test_telegram_html_split_preserves_a_long_single_line(self) -> None:
        source = "가" * 101
        formatted = common.format_codex_markdown_for_telegram(source)

        chunks = common.split_telegram_html(formatted, max_visible_units=20)

        probes = [probe_telegram_html(chunk) for chunk in chunks]
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(probe.visible_text) <= 20 for probe in probes))
        self.assertEqual("".join(probe.visible_text for probe in probes), source)

    def test_telegram_html_split_prefers_whitespace_boundaries(self) -> None:
        for source, expected_first_chunk in (
            ("alpha beta gamma", "alpha beta "),
            ("alpha beta\ngamma", "alpha beta\n"),
        ):
            with self.subTest(source=source):
                chunks = common.split_telegram_html(
                    common.format_codex_markdown_for_telegram(source),
                    max_visible_units=12,
                )
                visible_chunks = [
                    probe_telegram_html(chunk).visible_text for chunk in chunks
                ]

                self.assertEqual(visible_chunks[0], expected_first_chunk)
                self.assertEqual("".join(visible_chunks), source)

    def test_telegram_html_split_keeps_fenced_code_chunks_balanced(self) -> None:
        code = "\n".join(["print('<긴 코드>')"] * 12)
        formatted = common.format_codex_markdown_for_telegram(
            f"```python\n{code}\n```"
        )

        chunks = common.split_telegram_html(formatted, max_visible_units=35)

        probes = [probe_telegram_html(chunk) for chunk in chunks]
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(probe.visible_text) <= 35 for probe in probes))
        self.assertEqual("".join(probe.visible_text for probe in probes), code)

    def test_session_emoji_is_stable_and_distinguishes_sessions(self) -> None:
        marker = common.session_emoji("thread-stable")

        self.assertEqual(common.session_emoji("thread-stable"), marker)
        self.assertIn(marker, common.SESSION_EMOJIS)
        self.assertGreater(
            len({common.session_emoji(f"thread-{index}") for index in range(16)}),
            1,
        )
        self.assertEqual(common.session_emoji(""), "💬")

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

    def test_send_message_redacts_bracketed_and_unclosed_quoted_secrets(self) -> None:
        config = make_config("redaction-edge-cases")
        bracketed_secret = "correct-horse-battery-staple"
        unclosed_secret = "unterminated-secret-value"
        with mock.patch.object(
            common,
            "_telegram_api",
            return_value={"message_id": 8},
        ) as telegram_api:
            common.send_message(
                config,
                (
                    f"password=<{bracketed_secret}>\n"
                    f"api_key='{unclosed_secret}"
                ),
            )

        payload = telegram_api.call_args.args[2]
        self.assertNotIn(bracketed_secret, payload["text"])
        self.assertNotIn(unclosed_secret, payload["text"])
        self.assertGreaterEqual(payload["text"].count(common.REDACTION_MARKER), 2)

    def test_codex_notification_can_disable_notification_sound(self) -> None:
        config = make_config("silent-message")
        with mock.patch.object(
            common,
            "_telegram_api",
            return_value={"message_id": 9},
        ) as telegram_api:
            message_id = common.send_codex_notification(
                "진행 중",
                title="🦊 Codex 진행 · 작업 세션",
                config=config,
                silent=True,
            )

        self.assertEqual(message_id, 9)
        payload = telegram_api.call_args.args[2]
        self.assertIs(payload["disable_notification"], True)
        self.assertEqual(payload["parse_mode"], "HTML")

    def test_codex_notification_keeps_normal_sound_by_default(self) -> None:
        config = make_config("normal-message")
        with mock.patch.object(
            common,
            "_telegram_api",
            return_value={"message_id": 10},
        ) as telegram_api:
            common.send_codex_notification(
                "완료",
                title="🦊 ✅ Codex 완료 · 작업 세션",
                config=config,
            )

        payload = telegram_api.call_args.args[2]
        self.assertNotIn("disable_notification", payload)

    def test_long_codex_notification_sends_all_chunks_in_order_and_returns_first_id(
        self,
    ) -> None:
        config = make_config("long-message-order")
        source = "0123456789" * 1000
        next_message_id = 70

        def respond(
            _config: common.TelegramConfig,
            method: str,
            _payload: dict[str, object],
        ) -> dict[str, int]:
            nonlocal next_message_id
            self.assertEqual(method, "sendMessage")
            response = {"message_id": next_message_id}
            next_message_id += 1
            return response

        with mock.patch.object(common, "_telegram_api", side_effect=respond) as api:
            message_id = common.send_codex_notification(
                source,
                title="긴 결과",
                config=config,
            )

        payloads = [call.args[2] for call in api.call_args_list]
        bodies = [
            probe_telegram_html(str(payload["text"])).visible_text.partition("\n\n")[2]
            for payload in payloads
        ]
        self.assertGreater(len(payloads), 1)
        self.assertEqual(message_id, 70)
        self.assertEqual("".join(bodies), source)

    def test_long_codex_notification_numbers_each_repeated_title(self) -> None:
        config = make_config("long-message-titles")
        with mock.patch.object(
            common,
            "_telegram_api",
            side_effect=lambda *_args: {"message_id": 80},
        ) as api:
            common.send_codex_notification(
                "가" * 9000,
                title="작업 결과",
                config=config,
            )

        payloads = [call.args[2] for call in api.call_args_list]
        total = len(payloads)
        self.assertGreater(total, 1)
        for index, payload in enumerate(payloads, start=1):
            first_line = probe_telegram_html(
                str(payload["text"])
            ).visible_text.splitlines()[0]
            self.assertIn("작업 결과", first_line)
            self.assertRegex(first_line, rf"(?<!\d){index}/{total}(?!\d)")

    def test_long_codex_notification_preserves_telegram_delivery_contract(
        self,
    ) -> None:
        config = make_config("long-message-contract")
        secret = config.bot_token
        source = ("**중요🙂** 안전하게 전송\n" * 500) + f"token={secret}"
        with mock.patch.object(
            common,
            "_telegram_api",
            side_effect=lambda *_args: {"message_id": 90},
        ) as api:
            common.send_codex_notification(
                source,
                title=f"완료🙂 {secret}",
                config=config,
                silent=True,
            )

        payloads = [call.args[2] for call in api.call_args_list]
        self.assertGreater(len(payloads), 1)
        for payload in payloads:
            rendered = str(payload["text"])
            self.assertLessEqual(
                utf16_units(probe_telegram_html(rendered).visible_text),
                4096,
            )
            self.assertNotIn(secret, rendered)
            self.assertEqual(payload["parse_mode"], "HTML")
            self.assertIs(payload["disable_notification"], True)

    def test_codex_notification_rejects_a_title_that_exceeds_payload_limit(
        self,
    ) -> None:
        config = make_config("oversized-title")
        with mock.patch.object(common, "_telegram_api") as telegram_api:
            with self.assertRaises(common.TelegramBridgeError):
                common.send_codex_notification(
                    "본문",
                    title="제" * 4094,
                    config=config,
                )

        telegram_api.assert_not_called()

    def test_codex_notification_normalizes_an_unsplittable_budget_error(
        self,
    ) -> None:
        config = make_config("unsplittable-budget")
        with mock.patch.object(common, "_telegram_api") as telegram_api:
            with self.assertRaises(common.TelegramBridgeError):
                common.send_codex_notification(
                    "🙂",
                    title="제" * 4093,
                    config=config,
                )

        telegram_api.assert_not_called()

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

    def test_question_cancellation_closes_telegram_when_codex_answers_first(self) -> None:
        config = make_config("question-cancel")
        pending_dir = common.pending_question_dir(config)
        cancelled = threading.Event()

        def send_then_cancel(*_args: object, **_kwargs: object) -> int:
            cancelled.set()
            return 13

        with (
            mock.patch.object(common, "send_message", side_effect=send_then_cancel),
            mock.patch.object(common, "edit_message") as edit,
            mock.patch.object(common, "mirror_terminal_event"),
        ):
            answers = common.ask_questions(
                [
                    {
                        "header": "확인",
                        "question": "진행할까요?",
                        "options": [{"label": "예", "description": ""}],
                    }
                ],
                session_id="thread-cancel",
                server_url="http://127.0.0.1:43991/",
                timeout_seconds=5,
                config=config,
                session_name="동기화 테스트",
                cancel_event=cancelled,
            )

        self.assertIsNone(answers)
        edit.assert_called_once_with(
            config,
            13,
            "🧵 세션: 동기화 테스트\n\n✅ Codex에서 답변을 받았습니다.",
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
