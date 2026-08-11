from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import common  # noqa: E402
import setup  # noqa: E402


class SetupTests(unittest.TestCase):
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
