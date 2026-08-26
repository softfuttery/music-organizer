import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from music_organizer.auth import write_secret_atomic as real_write_secret_atomic
from music_organizer.config import (
    apply_defaults,
    load_config,
    migrate_plaintext_credentials,
    save_config,
)
from music_organizer.naming import (
    LEGACY_PATH_FORMAT,
    PICARD_PRESET3_PATH_FORMAT,
)


class ConfigDefaultsTests(unittest.TestCase):
    @staticmethod
    def credential_config(root: Path, prefix: str) -> dict:
        return {
            "qbittorrent": {
                "password": f"{prefix}-qb-password",
                "password_file": str(root / "qb-password"),
                "api_key": f"{prefix}-qb-api-key",
                "api_key_file": str(root / "qb-api-key"),
            },
            "review": {
                "proxy_password": f"{prefix}-proxy-password",
                "proxy_password_file": str(root / "proxy-password"),
            },
        }

    def test_failed_secret_write_rolls_back_earlier_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.yaml"
            save_config(config_path, self.credential_config(root, "old"))
            config_before = config_path.read_bytes()
            secret_paths = [
                root / "qb-password",
                root / "qb-api-key",
                root / "proxy-password",
            ]
            secrets_before = {path: path.read_bytes() for path in secret_paths}

            def fail_second_secret(path: Path, value: str) -> None:
                if path == root / "qb-api-key" and value.startswith("new-"):
                    raise OSError("simulated secret write failure")
                real_write_secret_atomic(path, value)

            with mock.patch(
                "music_organizer.config.write_secret_atomic",
                side_effect=fail_second_secret,
            ):
                with self.assertRaisesRegex(OSError, "simulated"):
                    save_config(config_path, self.credential_config(root, "new"))

            self.assertEqual(config_path.read_bytes(), config_before)
            self.assertEqual(
                {path: path.read_bytes() for path in secret_paths},
                secrets_before,
            )

    def test_failed_yaml_write_rolls_back_all_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.yaml"
            save_config(config_path, self.credential_config(root, "old"))
            config_before = config_path.read_bytes()
            secret_paths = [
                root / "qb-password",
                root / "qb-api-key",
                root / "proxy-password",
            ]
            secrets_before = {path: path.read_bytes() for path in secret_paths}

            with mock.patch(
                "music_organizer.config.yaml.safe_dump",
                side_effect=OSError("simulated yaml write failure"),
            ):
                with self.assertRaisesRegex(OSError, "simulated"):
                    save_config(config_path, self.credential_config(root, "new"))

            self.assertEqual(config_path.read_bytes(), config_before)
            self.assertEqual(
                {path: path.read_bytes() for path in secret_paths},
                secrets_before,
            )

    def test_secret_file_preserves_credential_whitespace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.yaml"
            password_file = root / "qb-password"
            password_file.write_text("  spaced password  \n", encoding="utf-8")
            config_path.write_text(
                yaml.safe_dump(
                    {
                        "qbittorrent": {
                            "password_file": str(password_file),
                        }
                    }
                ),
                encoding="utf-8",
            )

            loaded = load_config(config_path)

            self.assertEqual(
                loaded["qbittorrent"]["password"], "  spaced password  "
            )

    def test_translation_api_key_is_externalized_and_hydrated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.yaml"
            api_key_path = root / "secrets" / "translation-api-key"

            save_config(
                config_path,
                {
                    "translation": {
                        "enabled": True,
                        "api_key": "deepseek-secret",
                        "api_key_file": str(api_key_path),
                    }
                },
            )

            persisted_text = config_path.read_text(encoding="utf-8")
            persisted = yaml.safe_load(persisted_text)
            self.assertNotIn("deepseek-secret", persisted_text)
            self.assertNotIn("api_key", persisted["translation"])
            self.assertEqual(
                api_key_path.read_text(encoding="utf-8").strip(),
                "deepseek-secret",
            )
            self.assertEqual(
                load_config(config_path)["translation"]["api_key"],
                "deepseek-secret",
            )

    def test_malformed_proxy_port_cannot_bypass_credential_externalization(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.yaml"
            proxy_password_file = root / "secrets" / "proxy-password"
            config = {
                "review": {
                    "proxy_url": (
                        "http://legacy-user:do-not-persist@proxy.local:notaport"
                    ),
                    "proxy_password_file": str(proxy_password_file),
                }
            }

            save_config(config_path, config)

            persisted_text = config_path.read_text(encoding="utf-8")
            persisted = yaml.safe_load(persisted_text)
            self.assertNotIn("do-not-persist", persisted_text)
            self.assertEqual(
                persisted["review"]["proxy_url"],
                "http://proxy.local:notaport",
            )
            self.assertEqual(
                proxy_password_file.read_text(encoding="utf-8").strip(),
                "do-not-persist",
            )

    def test_qbittorrent_url_credentials_are_migrated_out_of_yaml(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.yaml"
            password_file = root / "secrets" / "qb-password"
            config = {
                "qbittorrent": {
                    "base_url": "http://legacy-user:legacy-password@qb.local:8080",
                    "password_file": str(password_file),
                }
            }

            save_config(config_path, config)

            persisted_text = config_path.read_text(encoding="utf-8")
            persisted = yaml.safe_load(persisted_text)
            self.assertNotIn("legacy-password", persisted_text)
            self.assertEqual(
                persisted["qbittorrent"]["base_url"],
                "http://qb.local:8080",
            )
            self.assertEqual(persisted["qbittorrent"]["username"], "legacy-user")
            self.assertEqual(
                password_file.read_text(encoding="utf-8").strip(),
                "legacy-password",
            )

    def test_legacy_plaintext_credentials_migrate_to_restricted_secret_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.yaml"
            qb_password_file = root / "secrets" / "qb-password"
            qb_api_key_file = root / "secrets" / "qb-api-key"
            proxy_password_file = root / "secrets" / "proxy-password"
            config_path.write_text(
                """
qbittorrent:
  password: legacy-qb-password
  api_key: legacy-qb-api-key
review:
  proxy_url: http://legacy-user:legacy-proxy-password@proxy.local:7890
""".lstrip(),
                encoding="utf-8",
            )
            environment = {
                "QBITTORRENT_PASSWORD_FILE": str(qb_password_file),
                "QBITTORRENT_API_KEY_FILE": str(qb_api_key_file),
                "REVIEW_PROXY_PASSWORD_FILE": str(proxy_password_file),
            }

            with mock.patch.dict(os.environ, environment):
                loaded = load_config(config_path)
                self.assertEqual(
                    loaded["qbittorrent"]["password"], "legacy-qb-password"
                )
                self.assertEqual(
                    loaded["qbittorrent"]["api_key"], "legacy-qb-api-key"
                )
                self.assertEqual(
                    loaded["review"]["proxy_url"], "http://proxy.local:7890"
                )
                self.assertEqual(loaded["review"]["proxy_username"], "legacy-user")
                self.assertEqual(
                    loaded["review"]["proxy_password"],
                    "legacy-proxy-password",
                )

                self.assertTrue(migrate_plaintext_credentials(config_path))
                reloaded = load_config(config_path)
                self.assertFalse(migrate_plaintext_credentials(config_path))

            persisted_text = config_path.read_text(encoding="utf-8")
            persisted = yaml.safe_load(persisted_text)
            for secret in (
                "legacy-qb-password",
                "legacy-qb-api-key",
                "legacy-proxy-password",
            ):
                self.assertNotIn(secret, persisted_text)
            self.assertNotIn("password", persisted["qbittorrent"])
            self.assertNotIn("api_key", persisted["qbittorrent"])
            self.assertNotIn("proxy_password", persisted["review"])
            self.assertEqual(
                qb_password_file.read_text(encoding="utf-8").strip(),
                "legacy-qb-password",
            )
            self.assertEqual(
                qb_api_key_file.read_text(encoding="utf-8").strip(),
                "legacy-qb-api-key",
            )
            self.assertEqual(
                proxy_password_file.read_text(encoding="utf-8").strip(),
                "legacy-proxy-password",
            )
            for path in (qb_password_file, qb_api_key_file, proxy_password_file):
                if os.name != "nt":
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(
                reloaded["qbittorrent"]["password"], "legacy-qb-password"
            )
            self.assertEqual(
                reloaded["qbittorrent"]["api_key"], "legacy-qb-api-key"
            )
            self.assertEqual(
                reloaded["review"]["proxy_password"],
                "legacy-proxy-password",
            )

    def test_legacy_beets_settings_are_migrated_into_review(self):
        config = apply_defaults(
            {
                "beets": {
                    "directory": "/legacy/library",
                    "library": "/data/legacy.db",
                    "config_path": "/data/legacy.yaml",
                    "path_format": LEGACY_PATH_FORMAT,
                    "import_mode": "copy",
                    "write_tags": True,
                },
                "review": {
                    "enabled": True,
                    "directory": "/review/library",
                    "library": "/data/review.db",
                },
            }
        )

        self.assertNotIn("beets", config)
        self.assertEqual(config["review"]["directory"], "/review/library")
        self.assertEqual(config["review"]["library"], "/data/review.db")
        self.assertEqual(config["review"]["config_path"], "/data/legacy.yaml")
        self.assertEqual(
            config["review"]["path_format"], PICARD_PRESET3_PATH_FORMAT
        )
        self.assertEqual(config["review"]["import_mode"], "copy")
        self.assertTrue(config["review"]["write_tags"])
        self.assertEqual(
            config["review"]["extra_file_patterns"], ["*.jpg", "*.png"]
        )
        self.assertFalse(config["review"]["move_extra_files"])
        self.assertFalse(config["review"]["cleanup_source_after_import"])
        self.assertEqual(config["review"]["import_timeout_seconds"], 3600)

    def test_review_values_take_precedence_over_legacy_beets(self):
        config = apply_defaults(
            {
                "beets": {"import_mode": "copy", "write_tags": True},
                "review": {
                    "import_mode": "move",
                    "write_tags": False,
                    "path_format": "custom/$title",
                },
            }
        )

        self.assertEqual(config["review"]["import_mode"], "move")
        self.assertFalse(config["review"]["write_tags"])
        self.assertEqual(config["review"]["path_format"], "custom/$title")


if __name__ == "__main__":
    unittest.main()
