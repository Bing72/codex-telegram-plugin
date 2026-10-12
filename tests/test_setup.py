from __future__ import annotations

import json
import os
import subprocess
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

    def test_refresh_keeps_unrelated_runtime_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            with (
                mock.patch.object(setup, "DEFAULT_SETTINGS_PATH", path),
                mock.patch.object(setup, "load_runtime_settings", return_value={"custom_toggle": "keep"}),
            ):
                setup.write_settings(
                    mode="shared", env_path=Path(directory) / "x.env",
                    shared_dir=Path(directory), receiver="external",
                )
            self.assertEqual(json.loads(path.read_text())["custom_toggle"], "keep")

    def test_refresh_keeps_existing_terminal_mirror_preference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            with (
                mock.patch.object(setup, "DEFAULT_SETTINGS_PATH", path),
                mock.patch.object(setup, "load_runtime_settings", return_value={"terminal_mirror": False}),
            ):
                setup.write_settings(
                    mode="standalone", env_path=Path(directory) / "x.env",
                    shared_dir=Path(directory), receiver="standalone",
                )
            self.assertFalse(json.loads(path.read_text())["terminal_mirror"])

    def test_install_hermes_enables_adapter_without_replacing_other_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            commands: list[list[str]] = []

            def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                commands.append(command)
                result = "builtin\n" if command[1:4] == ["config", "get", "security.approval.transport"] else ""
                return subprocess.CompletedProcess(command, 0, stdout=result, stderr="")

            with (
                mock.patch.object(setup.shutil, "which", side_effect=lambda name: "/bin/hermes" if name == "hermes" else None),
                mock.patch.object(setup, "hermes_home", return_value=home),
                mock.patch.object(setup.subprocess, "run", side_effect=run),
            ):
                self.assertTrue(setup.install_hermes())
                self.assertTrue(setup.install_hermes())
            folder = home / "plugins" / "codex-telegram-bridge"
            self.assertTrue((folder / "plugin.yaml").is_file())
            self.assertEqual(json.loads((folder / "bridge-source.json").read_text())["plugin_root"], str(ROOT))
            self.assertIn(["/bin/hermes", "plugins", "enable", "codex-telegram-bridge"], commands)
            self.assertIn(["/bin/hermes", "config", "set", "security.approval.transport", "codex-telegram"], commands)

    def test_install_hermes_does_not_override_other_approval_transport(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch.object(setup.shutil, "which", return_value="/bin/hermes"),
                mock.patch.object(setup, "hermes_home", return_value=Path(directory)),
                mock.patch.object(setup.subprocess, "run", side_effect=[
                    subprocess.CompletedProcess([], 0, "", ""),
                    subprocess.CompletedProcess([], 0, "custom-ui\n", ""),
                ]) as run,
            ):
                setup.install_hermes()
            self.assertEqual(run.call_count, 2)

    def test_install_hermes_skips_when_not_installed(self) -> None:
        with mock.patch.object(setup.shutil, "which", return_value=None):
            self.assertFalse(setup.install_hermes())

    def test_install_hermes_refuses_unmanaged_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "plugins" / "codex-telegram-bridge"
            folder.mkdir(parents=True)
            (folder / "__init__.py").write_text("# user code\n")
            with (
                mock.patch.object(setup.shutil, "which", return_value="/bin/hermes"),
                mock.patch.object(setup, "hermes_home", return_value=Path(directory)),
                self.assertRaises(common.TelegramBridgeError),
            ):
                setup.install_hermes()
            self.assertEqual((folder / "__init__.py").read_text(), "# user code\n")

    def test_install_hermes_preserves_user_edits_to_managed_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch.object(setup.shutil, "which", return_value="/bin/hermes"),
                mock.patch.object(setup, "hermes_home", return_value=Path(directory)),
                mock.patch.object(setup.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "builtin\n", "")),
            ):
                setup.install_hermes()
                plugin = Path(directory) / "plugins" / "codex-telegram-bridge" / "__init__.py"
                plugin.write_text("# locally customized\n")
                with self.assertRaises(common.TelegramBridgeError):
                    setup.install_hermes()
                self.assertEqual(plugin.read_text(), "# locally customized\n")

    def test_refresh_reapplies_hermes_without_changing_receiver(self) -> None:
        existing = {
            "mode": "shared", "env_path": "/credentials/existing.env",
            "shared_dir": "/shared", "receiver": "external",
        }
        with (
            mock.patch.object(sys, "argv", ["setup.py", "--refresh"]),
            mock.patch.object(setup, "load_runtime_settings", return_value=existing),
            mock.patch.object(Path, "is_file", return_value=True),
            mock.patch.object(setup, "validate_config"),
            mock.patch.object(setup, "install_launchers"),
            mock.patch.object(setup, "write_settings") as write,
            mock.patch.object(setup, "install_hermes", return_value=True) as hermes,
            mock.patch.object(setup, "install_services") as services,
        ):
            self.assertEqual(setup.main(), 0)
        write.assert_called_once_with(
            mode="shared", env_path=Path(existing["env_path"]),
            shared_dir=Path("/shared"), receiver="external",
        )
        hermes.assert_called_once()
        services.assert_not_called()

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
