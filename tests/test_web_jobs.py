import importlib
import os
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from music_organizer.auth import hash_password


class WebJobApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.root = root
        config_path = root / "config.yaml"
        config_path.write_text("paths_mapping: {}\n", encoding="utf-8")
        self.env = mock.patch.dict(
            os.environ,
            {
                "CONFIG_PATH": str(config_path),
                "DATABASE_PATH": str(root / "organizer.sqlite3"),
                "LOG_PATH": str(root / "organizer.log"),
                "SECRET_KEY": "test-secret-key",
                "AUTH_PASSWORD": "",
                "MAGICPUSH_TOKEN_FILE": str(root / "magicpush" / "token"),
                "QBITTORRENT_PASSWORD_FILE": str(root / "secrets" / "qb-password"),
                "QBITTORRENT_API_KEY_FILE": str(root / "secrets" / "qb-api-key"),
                "REVIEW_PROXY_PASSWORD_FILE": str(
                    root / "secrets" / "review-proxy-password"
                ),
            },
        )
        self.env.start()
        sys.modules.pop("app", None)
        self.module = importlib.import_module("app")
        self.client = self.module.app.test_client()

    def tearDown(self):
        for handler in list(self.module.organizer.logger.handlers):
            self.module.organizer.logger.removeHandler(handler)
            handler.close()
        sys.modules.pop("app", None)
        self.env.stop()
        self.tempdir.cleanup()

    def test_mutating_job_api_requires_csrf_and_persists_queue(self):
        self.assertEqual(self.client.post("/api/trigger").status_code, 400)
        csrf_response = self.client.get("/api/csrf")
        token = csrf_response.get_json()["token"]
        headers = {"X-CSRF-Token": token}

        queued = self.client.post("/api/trigger", headers=headers)
        duplicate = self.client.post("/api/qb/trigger", headers=headers)

        self.assertEqual(queued.status_code, 202)
        self.assertEqual(queued.get_json()["status"], "queued")
        self.assertEqual(duplicate.status_code, 409)
        self.assertTrue(self.client.get("/api/job").get_json()["running"])

        stopped = self.client.post("/api/stop", headers=headers)
        self.assertEqual(stopped.status_code, 202)
        self.assertEqual(stopped.get_json()["status"], "cancelled")

    def test_qb_attention_item_can_be_reset_and_queued_for_manual_retry(self):
        torrent = {"hash": "ABC", "name": "Album", "progress": 1}
        self.module.organizer.repository.record_qb_failures(
            [torrent],
            "target conflict",
            max_attempts=1,
            base_delay_seconds=60,
            max_delay_seconds=300,
        )
        self.module.organizer.set_app_state_value("qb_sync_rid", "42")
        token = self.client.get("/api/csrf").get_json()["token"]

        response = self.client.post(
            "/api/qb/retry/ABC",
            headers={"X-CSRF-Token": token},
        )

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.get_json()["retry_reset"])
        self.assertEqual(response.get_json()["job_type"], "qb_poll")
        self.assertEqual(self.module.organizer.app_state_value("qb_sync_rid"), "0")

    def test_web_health_does_not_require_worker_heartbeat(self):
        response = self.client.get("/api/health?component=web")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "ok")
        self.assertFalse(hasattr(self.module, "scheduler"))

    def test_aggregate_health_ignores_review_worker_when_review_is_disabled(self):
        now = self.module.datetime.now().isoformat(timespec="seconds")
        self.module.organizer.set_app_state_value("worker_heartbeat", now)

        response = self.client.get("/api/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["worker"], "ok")
        self.assertEqual(response.get_json()["review_worker"], "disabled")

    def test_health_reports_review_worker_staleness_and_stuck_imports(self):
        config = self.module.organizer.load_config()
        config["review"]["enabled"] = True
        self.module.organizer.save_config(config)
        now = self.module.datetime.now().isoformat(timespec="seconds")
        self.module.organizer.set_app_state_value("worker_heartbeat", now)
        self.module.organizer.set_app_state_value("review_worker_heartbeat", now)

        healthy = self.client.get("/api/health")
        self.assertEqual(healthy.status_code, 200)
        self.assertEqual(healthy.get_json()["worker"], "ok")
        self.assertEqual(healthy.get_json()["review_worker"], "ok")

        self.module.organizer.set_app_state_value(
            "review_import_active_at",
            (
                self.module.datetime.now() - timedelta(minutes=5)
            ).isoformat(timespec="seconds"),
        )
        self.module.organizer.set_app_state_value(
            "review_import_timeout_seconds", "60"
        )
        stuck = self.client.get("/api/health?component=review-worker")
        self.assertEqual(stuck.status_code, 503)
        self.assertEqual(stuck.get_json()["status"], "degraded")
        self.assertEqual(stuck.get_json()["review_worker"], "stuck")
        self.assertEqual(self.client.get("/api/health?component=web").status_code, 200)
        self.assertEqual(
            self.client.get("/api/health?component=unknown").status_code, 400
        )

    def test_config_credentials_are_secret_files_and_blank_fields_preserve_them(self):
        csrf = self.client.get("/api/csrf").get_json()["token"]
        submitted = {
            "csrf_token": csrf,
            "paths_mapping": "/incoming => /library",
            "schedule_cron": "*/15 * * * *",
            "qb_base_url": "http://qb.local:8080",
            "qb_password": "private-qb-password",
            "qb_api_key": "private-qb-api-key",
            "review_proxy_url": "http://proxy.local:7890",
            "review_proxy_username": "proxy-user",
            "review_proxy_password": "private-proxy-password",
        }

        response = self.client.post("/config", data=submitted)
        self.assertEqual(response.status_code, 302)
        serialized = Path(self.module.CONFIG_PATH).read_text(encoding="utf-8")
        for secret in (
            "private-qb-password",
            "private-qb-api-key",
            "private-proxy-password",
        ):
            self.assertNotIn(secret, serialized)
        secret_paths = {
            "QBITTORRENT_PASSWORD_FILE": "private-qb-password",
            "QBITTORRENT_API_KEY_FILE": "private-qb-api-key",
            "REVIEW_PROXY_PASSWORD_FILE": "private-proxy-password",
        }
        for variable, expected in secret_paths.items():
            path = Path(os.environ[variable])
            self.assertEqual(path.read_text(encoding="utf-8").strip(), expected)
            if os.name == "posix":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

        submitted.update(
            {
                "qb_password": "",
                "qb_api_key": "",
                "review_proxy_password": "",
            }
        )
        self.assertEqual(self.client.post("/config", data=submitted).status_code, 302)
        for variable, expected in secret_paths.items():
            self.assertEqual(
                Path(os.environ[variable]).read_text(encoding="utf-8").strip(),
                expected,
            )
        page = str(self.client.get("/api/config").get_json())
        for secret in secret_paths.values():
            self.assertNotIn(secret, page)

    def test_vue_config_api_validates_saves_and_returns_fresh_values(self):
        csrf = self.client.get("/api/csrf").get_json()["token"]
        response = self.client.post(
            "/api/config",
            data={
                "paths_mapping": "/incoming => /library",
                "mode": "copy",
                "schedule_cron": "*/20 * * * *",
                "schedule_enabled": "on",
            },
            headers={"X-CSRF-Token": csrf},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["values"]["mode"], "copy")
        self.assertEqual(payload["values"]["schedule_cron"], "*/20 * * * *")
        self.assertTrue(payload["values"]["schedule_enabled"])
        self.assertIn("Worker", payload["message"])

    def test_session_login_protects_control_plane_without_basic_auth_challenge(self):
        secret_hash = hash_password("secret")
        changed_hash = hash_password("changed")
        with mock.patch.dict(
            os.environ,
            {"AUTH_USERNAME": "admin", "AUTH_PASSWORD_HASH": secret_hash},
        ):
            unauthorized = self.client.get("/api/job")
            self.assertEqual(unauthorized.status_code, 401)
            self.assertNotIn("WWW-Authenticate", unauthorized.headers)
            self.assertFalse(self.client.get("/api/session").get_json()["authenticated"])

            csrf_token = self.client.get("/api/csrf").get_json()["token"]
            bad_login = self.client.post(
                "/api/login",
                json={"username": "admin", "password": "wrong"},
                headers={"X-CSRF-Token": csrf_token},
            )
            self.assertEqual(bad_login.status_code, 401)

            login = self.client.post(
                "/api/login",
                json={"username": "admin", "password": "secret"},
                headers={"X-CSRF-Token": csrf_token},
            )
            self.assertEqual(login.status_code, 200)
            self.assertEqual(self.client.get("/api/job").status_code, 200)
            with mock.patch.dict(os.environ, {"AUTH_PASSWORD_HASH": changed_hash}):
                self.assertEqual(self.client.get("/api/job").status_code, 401)
            self.assertEqual(self.client.get("/api/job").status_code, 200)

            logout = self.client.post(
                "/api/logout",
                headers={"X-CSRF-Token": login.get_json()["csrf_token"]},
            )
            self.assertEqual(logout.status_code, 200)
            self.assertEqual(self.client.get("/api/job").status_code, 401)

    def test_pwa_shell_assets_remain_public_when_authentication_is_enabled(self):
        secret_hash = hash_password("secret")
        frontend_dist = Path(self.tempdir.name) / "frontend-dist"
        frontend_dist.mkdir()
        assets = {
            "manifest.webmanifest": "{}",
            "sw.js": "self.addEventListener('fetch', () => {})",
            "app-icon.svg": "<svg xmlns='http://www.w3.org/2000/svg'/>",
        }
        for name, content in assets.items():
            (frontend_dist / name).write_text(content, encoding="utf-8")

        with (
            mock.patch.dict(
                os.environ,
                {"AUTH_USERNAME": "admin", "AUTH_PASSWORD_HASH": secret_hash},
            ),
            mock.patch.object(self.module, "FRONTEND_DIST", frontend_dist),
        ):
            for path in assets:
                response = self.client.get(f"/{path}")
                self.assertEqual(response.status_code, 200, path)
                self.assertNotIn("Location", response.headers)
            service_worker = self.client.get("/sw.js")
            self.assertEqual(service_worker.headers["Service-Worker-Allowed"], "/")
            self.assertEqual(service_worker.headers["Cache-Control"], "no-cache")

    def test_frontend_dist_prefers_packaged_assets_and_falls_back_to_vite_build(self):
        project_root = Path(self.module.__file__).resolve().parent
        with (
            mock.patch.dict(os.environ, {"FRONTEND_DIST": ""}),
            mock.patch.object(Path, "is_file", return_value=True),
        ):
            self.assertEqual(
                self.module.resolve_frontend_dist(), project_root / "frontend_dist"
            )
        with (
            mock.patch.dict(os.environ, {"FRONTEND_DIST": ""}),
            mock.patch.object(Path, "is_file", return_value=False),
        ):
            self.assertEqual(
                self.module.resolve_frontend_dist(),
                project_root / "frontend-vue" / "dist",
            )

        configured = self.root / "custom-frontend"
        with mock.patch.dict(os.environ, {"FRONTEND_DIST": str(configured)}):
            self.assertEqual(self.module.resolve_frontend_dist(), configured)

    def test_session_login_password_can_be_loaded_from_a_read_only_secret_file(self):
        secret_path = Path(self.tempdir.name) / "auth_password"
        secret_path.write_text(hash_password("file-secret") + "\n", encoding="utf-8")
        with mock.patch.dict(
            os.environ,
            {"AUTH_PASSWORD_HASH_FILE": str(secret_path), "AUTH_USERNAME": "admin"},
        ):
            os.environ.pop("AUTH_PASSWORD", None)
            self.assertEqual(self.client.get("/api/job").status_code, 401)
            csrf_token = self.client.get("/api/csrf").get_json()["token"]
            response = self.client.post(
                "/api/login",
                json={"username": "admin", "password": "file-secret"},
                headers={"X-CSRF-Token": csrf_token},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.client.get("/api/job").status_code, 200)
            self.assertNotIn("file-secret", secret_path.read_text(encoding="utf-8"))

    def test_hash_file_rejects_legacy_plaintext(self):
        secret_path = Path(self.tempdir.name) / "auth_password"
        secret_path.write_text("legacy-plaintext\n", encoding="utf-8")
        with mock.patch.dict(
            os.environ,
            {"AUTH_PASSWORD_HASH_FILE": str(secret_path), "AUTH_USERNAME": "admin"},
        ):
            os.environ.pop("AUTH_PASSWORD", None)
            response = self.client.get("/api/job")
            self.assertEqual(response.status_code, 503)
            self.assertIn("not a password hash", response.get_json()["error"])

    def test_magicpush_test_endpoint_is_csrf_protected_and_never_renders_token(self):
        token_path = self.root / "magicpush" / "token"
        token_path.parent.mkdir()
        token_path.write_text("private-test-token\n", encoding="utf-8")
        config = self.module.organizer.load_config()
        config["notifications"]["magicpush"].update(
            {
                "enabled": True,
                "base_url": "http://magicpush:818",
                "title": "Music Organizer",
            }
        )
        self.module.organizer.save_config(config)

        page = self.client.get("/api/config")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn(b"private-test-token", page.data)
        self.assertTrue(page.get_json()["saved"]["magicpush_token"])
        self.assertEqual(
            self.client.post("/api/notifications/magicpush/test").status_code,
            400,
        )

        csrf = self.client.get("/api/csrf").get_json()["token"]
        with mock.patch(
            "app.send_magicpush",
            return_value={"sent": True, "status_code": 200},
        ) as sender:
            response = self.client.post(
                "/api/notifications/magicpush/test",
                headers={"X-CSRF-Token": csrf},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["sent"])
        self.assertNotIn("private-test-token", str(sender.call_args))

    def test_config_only_exposes_review_driven_beets_import(self):
        page = self.client.get("/api/config")
        payload = page.get_json()

        self.assertEqual(page.status_code, 200)
        values = payload["values"]
        for name in (
            "review_import_mode",
            "review_path_format",
            "review_write_tags",
            "review_move_extra_files",
            "review_extra_file_patterns",
            "review_cleanup_source_after_import",
            "review_recycle_directory",
        ):
            self.assertIn(name, values)
        self.assertNotIn("beets_enabled", values)
        self.assertNotIn("beets_batch_size", values)

    def test_dashboard_reports_review_queue_instead_of_automatic_beets_queue(self):
        payload = self.client.get("/api/stats").get_json()

        self.assertEqual(payload["review_active"], 0)
        self.assertEqual(payload["review_archived"], 0)
        self.assertNotIn("beets_pending", payload)


if __name__ == "__main__":
    unittest.main()
