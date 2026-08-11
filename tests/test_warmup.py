from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import warmup  # noqa: E402


class WarmupTests(unittest.TestCase):
    def test_validate_local_url_accepts_loopback(self) -> None:
        url = "http://127.0.0.1:4097/session"
        self.assertEqual(warmup.validate_local_url(url), url)

    def test_validate_local_url_rejects_remote_hosts(self) -> None:
        with self.assertRaisesRegex(ValueError, "localhost"):
            warmup.validate_local_url("https://example.com/session")


if __name__ == "__main__":
    unittest.main()
