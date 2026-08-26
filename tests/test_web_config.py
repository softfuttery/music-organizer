import tempfile
import unittest
from pathlib import Path

from music_organizer.web_config import build_web_config


class WebConfigTests(unittest.TestCase):
    def test_build_preserves_secrets_and_normalizes_submitted_values(self):
        with tempfile.TemporaryDirectory() as directory:
            result = build_web_config(
                {
                    "qbittorrent": {
                        "password": "saved-password",
                        "api_key": "saved-key",
                    },
                    "review": {"import_timeout_seconds": 7200},
                },
                {
                    "paths_mapping": "/incoming => /library",
                    "mode": "hardlink",
                    "qb_base_url": "http://qb/",
                    "schedule_cron": "*/15 * * * *",
                    "review_proxy_url": "http://proxy.local:7890/",
                    "review_proxy_username": "proxy-user",
                    "review_proxy_password": "proxy-secret",
                    "review_recycle_directory": "/volume2/media/#recycle/music-organizer",
                    "review_auto_discover": "on",
                    "review_discovery_stable_seconds": "90",
                    "translation_enabled": "on",
                    "translation_base_url": "https://api.deepseek.com/",
                    "translation_model": "deepseek-v4-flash",
                    "translation_api_key": "translation-secret",
                    "translation_style": "lyrical",
                    "translation_timeout": "90",
                },
                Path(directory) / "token",
            )

        self.assertEqual(result["paths_mapping"], {"/incoming": "/library"})
        self.assertEqual(result["qbittorrent"]["base_url"], "http://qb")
        self.assertEqual(result["qbittorrent"]["password"], "saved-password")
        self.assertEqual(result["qbittorrent"]["api_key"], "saved-key")
        self.assertEqual(result["review"]["proxy_url"], "http://proxy.local:7890")
        self.assertEqual(result["review"]["proxy_username"], "proxy-user")
        self.assertEqual(result["review"]["proxy_password"], "proxy-secret")
        self.assertEqual(
            result["review"]["recycle_directory"],
            "/volume2/media/#recycle/music-organizer",
        )
        self.assertTrue(result["review"]["auto_discover"])
        self.assertEqual(result["review"]["discovery_stable_seconds"], 90)
        self.assertEqual(result["review"]["import_timeout_seconds"], 7200)
        self.assertTrue(result["translation"]["enabled"])
        self.assertEqual(
            result["translation"]["base_url"], "https://api.deepseek.com"
        )
        self.assertEqual(result["translation"]["api_key"], "translation-secret")
        self.assertEqual(result["translation"]["style"], "lyrical")
        self.assertEqual(result["translation"]["timeout"], 90)

        preserved = build_web_config(
            {
                "qbittorrent": {},
                "review": {"proxy_password": "saved-proxy-secret"},
            },
            {
                "paths_mapping": "/incoming => /library",
                "schedule_cron": "*/15 * * * *",
            },
            Path("/missing/token"),
        )
        self.assertEqual(
            preserved["review"]["proxy_password"], "saved-proxy-secret"
        )

    def test_relative_mapping_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "绝对路径"):
            build_web_config(
                {"qbittorrent": {}, "review": {}},
                {"paths_mapping": "incoming => /library"},
                Path("/missing/token"),
            )

    def test_invalid_proxy_port_is_rejected_before_save(self):
        with self.assertRaisesRegex(ValueError, "代理地址端口无效"):
            build_web_config(
                {"qbittorrent": {}, "review": {}},
                {
                    "paths_mapping": "/incoming => /library",
                    "review_proxy_url": "http://proxy.local:notaport",
                },
                Path("/missing/token"),
            )

    def test_relative_review_recycle_directory_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "回收站目录.*绝对路径"):
            build_web_config(
                {"qbittorrent": {}, "review": {}},
                {
                    "paths_mapping": "/incoming => /library",
                    "review_recycle_directory": "#recycle/music-organizer",
                },
                Path("/missing/token"),
            )

    def test_enabled_translation_requires_key_and_valid_url(self):
        with self.assertRaisesRegex(ValueError, "必须填写接口地址、模型和 API Key"):
            build_web_config(
                {"qbittorrent": {}, "review": {}, "translation": {}},
                {
                    "paths_mapping": "/incoming => /library",
                    "translation_enabled": "on",
                    "translation_base_url": "https://api.deepseek.com",
                    "translation_model": "deepseek-v4-flash",
                },
                Path("/missing/token"),
            )


if __name__ == "__main__":
    unittest.main()
