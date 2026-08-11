#!/usr/bin/env python3
"""Trigger lazy OpenCode plugin initialization through a localhost request."""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request


def validate_local_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in {
        "127.0.0.1",
        "::1",
        "localhost",
    }:
        raise ValueError("warmup URL must be localhost HTTP")
    return url


def warmup(url: str, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                if response.status != 200:
                    raise RuntimeError(f"warmup returned HTTP {response.status}")
                json.load(response)
                return
        except (
            OSError,
            RuntimeError,
            TimeoutError,
            json.JSONDecodeError,
            urllib.error.URLError,
        ) as exc:
            last_error = exc
            time.sleep(0.5)
    raise RuntimeError(f"OpenCode warmup failed: {last_error}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:4097/session")
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()
    warmup(validate_local_url(args.url), args.timeout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
