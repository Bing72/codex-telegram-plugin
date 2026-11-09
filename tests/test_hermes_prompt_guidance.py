from __future__ import annotations

import unittest
from unittest import mock

from test_hermes_plugin import load_plugin


class HermesPromptGuidanceTests(unittest.TestCase):
    def test_guidance_is_session_policy_not_repeated_user_context(self) -> None:
        try:
            from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
        except ImportError:
            self.skipTest("Run with hermes --run-module unittest")
        plugin = load_plugin()
        manager = PluginManager()
        ctx = PluginContext(PluginManifest(name="telegram-guidance-test"), manager)
        try:
            with (mock.patch.object(ctx, "register_tool"),
                  mock.patch.object(ctx, "register_middleware"),
                  mock.patch.object(ctx, "register_approval_transport")):
                plugin.register(ctx)
            for first_turn in (True, False):
                self.assertEqual(manager.invoke_hook(
                    "pre_llm_call", user_message="하이", is_first_turn=first_turn,
                ), [])
            sections = manager.render_system_prompt_sections({"platform": "cli"})
            self.assertEqual(len(sections), 1)
            self.assertEqual(sections[0].content, plugin.HermesTelegramAdapter.question_guidance())
            self.assertIn("hermes_telegram_ask_user", sections[0].content)
            self.assertIn("승인", sections[0].content)
        finally:
            for handle in reversed(manager._registration_order[:]):
                handle.dispose()


if __name__ == "__main__":
    unittest.main()
