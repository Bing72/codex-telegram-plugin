from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import common  # noqa: E402
import setup  # noqa: E402


class SetupTests(unittest.TestCase):
    def test_write_settings_preserves_telegram_choice_routing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            with (
                mock.patch.object(setup, "DEFAULT_SETTINGS_PATH", path),
                mock.patch.object(
                    setup,
                    "load_runtime_settings",
                    return_value={"question_routing": "telegram_choices"},
                ),
            ):
                setup.write_settings(
                    mode="shared",
                    env_path=Path(directory) / ".env",
                    shared_dir=Path(directory),
                    receiver="opencode",
                )

            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["question_routing"],
                "telegram_choices",
            )

    def test_validate_config_rejects_group_readable_credentials(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX mode validation")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "telegram.env"
            path.write_text(
                "\n".join(
                    [
                        "TELEGRAM_BOT_TOKEN=123456:test",
                        "TELEGRAM_CHAT_ID=42",
                        "TELEGRAM_ALLOWED_USER_IDS=42",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            path.chmod(0o640)
            with self.assertRaisesRegex(
                common.TelegramBridgeError,
                "group/world accessible",
            ):
                setup.validate_config(path)

            path.chmod(0o600)
            setup.validate_config(path)


if __name__ == "__main__":
    unittest.main()
