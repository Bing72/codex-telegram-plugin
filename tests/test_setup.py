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
    def test_manifest_only_advertises_supported_integrations(self) -> None:
        manifest = json.loads((ROOT / ".codex-plugin/plugin.json").read_text())
        self.assertNotIn("opencode", json.dumps(manifest).lower())
        self.assertIn("standalone", manifest["description"])

    def test_removed_credential_source_is_rejected(self) -> None:
        with (
            mock.patch.object(sys, "argv", ["setup.py", "--mode", "shared", "--reuse-opencode"]),
            mock.patch("sys.stderr"),
            self.assertRaises(SystemExit) as error,
        ):
            setup.parse_args()
        self.assertEqual(error.exception.code, 2)

    def test_shared_env_file_has_no_legacy_runtime_directory(self) -> None:
        env_path = Path.home() / ".config/opencode/telegram-remote/.env"
        with (
            mock.patch.object(sys, "argv", ["setup.py", "--mode", "shared", "--env-file", str(env_path)]),
            mock.patch.object(Path, "is_file", return_value=True),
            mock.patch.object(setup, "validate_config"),
            mock.patch.object(setup, "install_launchers"),
            mock.patch.object(setup, "write_settings") as write,
            mock.patch.object(setup, "install_hermes", return_value=False),
        ):
            self.assertEqual(setup.main(), 0)
        write.assert_called_once_with(
            mode="shared", env_path=env_path.resolve(),
            receiver="external",
        )

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
                    receiver="external",
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
                    receiver="external",
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
                    receiver="standalone",
                )
            self.assertFalse(json.loads(path.read_text())["terminal_mirror"])

    def test_services_only_manage_codex_units(self) -> None:
        for mode, receiver in [("shared", "external"), ("standalone", "standalone")]:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                with (
                    mock.patch.object(Path, "home", return_value=Path(directory)),
                    mock.patch.object(setup.shutil, "which", side_effect=lambda name: f"/bin/{name}") as which,
                    mock.patch.object(setup, "systemctl") as systemctl,
                ):
                    setup.install_services(mode, receiver)
                self.assertEqual(which.call_args_list, [mock.call("python3"), mock.call("codex")])
                units = Path(directory) / ".config/systemd/user"
                self.assertEqual({path.name for path in units.iterdir()}, {
                    "codex-telegram-bridge.service", "codex-telegram-poller.service",
                })
                expected = [
                    mock.call("daemon-reload"),
                    mock.call("enable", "codex-telegram-bridge.service"),
                    mock.call("restart", "codex-telegram-bridge.service"),
                ]
                if mode == "standalone":
                    expected.extend([
                        mock.call("enable", "codex-telegram-poller.service"),
                        mock.call("restart", "codex-telegram-poller.service"),
                    ])
                else:
                    expected.append(mock.call("disable", "--now", "codex-telegram-poller.service", check=False))
                self.assertEqual(systemctl.call_args_list, expected)
        self.assertFalse(hasattr(setup, "opencode_unit"))

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
            "receiver": "external",
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
            receiver="external",
        )
        hermes.assert_called_once()
        services.assert_not_called()

    def test_refresh_rejects_unsupported_pairs_before_credentials_or_writes(self) -> None:
        for mode, receiver in [
            ("shared", "opencode"), ("standalone", "opencode"),
            ("shared", "standalone"), ("standalone", "external"),
            ("unknown", "external"), ("shared", "unknown"),
        ]:
            with (
                self.subTest(mode=mode, receiver=receiver),
                mock.patch.object(sys, "argv", ["setup.py", "--refresh", "--install-services"]),
                mock.patch.object(setup, "load_runtime_settings", return_value={
                    "mode": mode, "receiver": receiver, "env_path": "/not-read.env",
                }),
                mock.patch.object(Path, "is_file") as exists,
                mock.patch.object(setup, "validate_config") as validate,
                mock.patch.object(setup, "install_launchers") as launchers,
                mock.patch.object(setup, "write_settings") as write,
                mock.patch.object(setup, "install_hermes") as hermes,
                mock.patch.object(setup, "install_services") as services,
            ):
                with self.assertRaisesRegex(common.TelegramBridgeError, "[Rr]econfigure.*--mode.*--env-file"):
                    setup.main()
                for boundary in (exists, validate, launchers, write, hermes, services):
                    boundary.assert_not_called()

    def test_refresh_standalone_without_shared_directory(self) -> None:
        with (
            mock.patch.object(sys, "argv", ["setup.py", "--refresh"]),
            mock.patch.object(setup, "load_runtime_settings", return_value={
                "mode": "standalone", "receiver": "standalone", "env_path": "/fixture.env",
            }),
            mock.patch.object(Path, "is_file", return_value=True),
            mock.patch.object(setup, "validate_config"),
            mock.patch.object(setup, "install_launchers"),
            mock.patch.object(setup, "write_settings") as write,
            mock.patch.object(setup, "install_hermes", return_value=False),
        ):
            self.assertEqual(setup.main(), 0)
        write.assert_called_once_with(mode="standalone", env_path=Path("/fixture.env"), receiver="standalone")

    def test_write_settings_drops_obsolete_shared_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            with (
                mock.patch.object(setup, "DEFAULT_SETTINGS_PATH", path),
                mock.patch.object(setup, "load_runtime_settings", return_value={
                    "shared_dir": "/obsolete", "custom_toggle": "keep",
                }),
            ):
                setup.write_settings(mode="shared", env_path=Path(directory) / "x.env", receiver="external")
            payload = json.loads(path.read_text())
            self.assertNotIn("shared_dir", payload)
            self.assertEqual(payload["custom_toggle"], "keep")

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
