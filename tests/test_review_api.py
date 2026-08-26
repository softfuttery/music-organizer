import importlib
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


class ReviewApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.inbox = self.root / "inbox"
        self.inbox.mkdir()
        self.album = self.inbox / "中文艺术家 - 中文专辑"
        self.album.mkdir()
        (self.album / "01 第一首.flac").write_bytes(b"audio")
        (self.album / "cover.jpg").write_bytes(b"image")
        self.recycle = self.root / "#recycle" / "music-organizer"
        self.recycle.parent.mkdir()
        self.library = self.root / "library"
        self.library.mkdir()
        (self.library / "existing.flac").write_bytes(b"library-audio")
        config_path = self.root / "config.yaml"
        config_path.write_text(
            "paths_mapping: {}\n"
            "review:\n"
            "  enabled: true\n"
            "  source_roots:\n"
            f"    - {self.inbox.as_posix()!r}\n"
            f"  directory: {self.library.as_posix()!r}\n",
            encoding="utf-8",
        )
        config_text = config_path.read_text(encoding="utf-8")
        config_path.write_text(
            config_text
            + f"  recycle_directory: {self.recycle.as_posix()!r}\n",
            encoding="utf-8",
        )
        self.env = mock.patch.dict(
            os.environ,
            {
                "CONFIG_PATH": str(config_path),
                "DATABASE_PATH": str(self.root / "organizer.sqlite3"),
                "LOG_PATH": str(self.root / "organizer.log"),
                "SECRET_KEY": "review-api-test",
                "AUTH_PASSWORD": "",
                "MAGICPUSH_TOKEN_FILE": str(self.root / "magicpush" / "token"),
            },
        )
        self.env.start()
        apscheduler = types.ModuleType("apscheduler")
        triggers = types.ModuleType("apscheduler.triggers")
        cron = types.ModuleType("apscheduler.triggers.cron")
        cron.CronTrigger = object
        self.scheduler_modules = mock.patch.dict(
            sys.modules,
            {
                "apscheduler": apscheduler,
                "apscheduler.triggers": triggers,
                "apscheduler.triggers.cron": cron,
            },
        )
        self.scheduler_modules.start()
        sys.modules.pop("app", None)
        self.module = importlib.import_module("app")
        self.client = self.module.app.test_client()

    def tearDown(self):
        for handler in list(self.module.organizer.logger.handlers):
            self.module.organizer.logger.removeHandler(handler)
            handler.close()
        sys.modules.pop("app", None)
        self.scheduler_modules.stop()
        self.env.stop()
        self.tempdir.cleanup()

    def test_browse_and_create_persistent_review_batch(self):
        roots = self.client.get("/api/review/roots").get_json()
        self.assertTrue(roots["enabled"])
        self.assertEqual(roots["roots"][0]["path"], str(self.inbox))

        listing = self.client.get(
            "/api/review/roots", query_string={"path": str(self.inbox)}
        ).get_json()
        self.assertEqual(listing["directories"][0]["name"], self.album.name)

        preview = self.client.get(
            "/api/review/files", query_string={"path": str(self.album)}
        ).get_json()
        self.assertEqual(preview["total"], 1)
        self.assertEqual(preview["files"][0]["extension"], ".flac")
        self.assertEqual(preview["files"][0]["relative_path"], "01 第一首.flac")

        csrf = self.client.get("/api/csrf").get_json()["token"]
        response = self.client.post(
            "/api/review/batches",
            json={"label": "中文测试", "paths": [str(self.album)]},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(response.status_code, 202)
        batch = response.get_json()
        self.assertEqual(batch["items"][0]["status"], "queued")
        stored = self.client.get(
            f"/api/review/batches/{batch['id']}"
        ).get_json()
        self.assertEqual(stored["label"], "中文测试")

    def test_shared_translation_endpoint_returns_validated_draft(self):
        csrf = self.client.get("/api/csrf").get_json()["token"]
        translated = {
            "content": "[00:01.00]こんにちは\n[00:01.00]你好",
            "translated_lines": 1,
        }
        with mock.patch.object(
            self.module, "LyricsTranslationService"
        ) as service_class:
            service_class.return_value.translate.return_value = translated
            response = self.client.post(
                "/api/lyrics/translate",
                json={
                    "content": "[00:01.00]こんにちは",
                    "title": "标题",
                    "artist": "艺术家",
                },
                headers={"X-CSRF-Token": csrf},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), translated)
        service_class.return_value.translate.assert_called_once_with(
            "[00:01.00]こんにちは", title="标题", artist="艺术家"
        )

    def test_config_api_shows_translation_settings_without_echoing_api_key(self):
        config = self.module.organizer.load_config()
        config["translation"]["api_key"] = "never-render-this-key"
        self.module.organizer.save_config(config)

        response = self.client.get("/api/config")
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertIn("translation_base_url", payload["values"])
        self.assertIn("translation_model", payload["values"])
        self.assertEqual(payload["values"]["translation_api_key"], "")
        self.assertTrue(payload["saved"]["translation_api_key"])
        self.assertNotIn("never-render-this-key", str(payload))

    def test_batch_rejects_directory_outside_allowed_root(self):
        csrf = self.client.get("/api/csrf").get_json()["token"]
        response = self.client.post(
            "/api/review/batches",
            json={"paths": [str(self.root)]},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("不在允许范围", response.get_json()["error"])

        preview = self.client.get(
            "/api/review/files", query_string={"path": str(self.root)}
        )
        self.assertEqual(preview.status_code, 400)

    def test_batch_reports_active_path_conflict(self):
        csrf = self.client.get("/api/csrf").get_json()["token"]
        first = self.client.post(
            "/api/review/batches",
            json={"paths": [str(self.album)]},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(first.status_code, 202)

        duplicate = self.client.post(
            "/api/review/batches",
            json={"paths": [str(self.album)]},
            headers={"X-CSRF-Token": csrf},
        )

        self.assertEqual(duplicate.status_code, 409)
        self.assertIn("已有活跃预审任务", duplicate.get_json()["error"])

    def test_recycle_review_source_moves_album_and_archives_item(self):
        csrf = self.client.get("/api/csrf").get_json()["token"]
        created = self.client.post(
            "/api/review/batches",
            json={"paths": [str(self.album)]},
            headers={"X-CSRF-Token": csrf},
        ).get_json()
        item_id = created["items"][0]["id"]
        claimed = self.module.review_repository.claim_next()
        self.module.review_repository.complete_identification(
            claimed["queue_id"],
            claimed["item_id"],
            signature="v1",
            audio_count=1,
            current_artist="艺术家",
            current_album="专辑",
            recommendation="none",
            candidates=[],
        )

        response = self.client.post(
            f"/api/review/items/{item_id}/recycle-source",
            json={"confirm_path": str(self.album)},
            headers={"X-CSRF-Token": csrf},
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.album.exists())
        recycled = list(self.recycle.iterdir())
        self.assertEqual(len(recycled), 1)
        self.assertTrue((recycled[0] / "01 第一首.flac").is_file())
        item = response.get_json()
        self.assertEqual(item["status"], "skipped")
        self.assertEqual(item["import_result"]["outcome"], "source_recycled")
        self.assertEqual(
            item["import_result"]["recycle_destination"], str(recycled[0])
        )
        self.assertTrue(item["archived"])

    def test_recycle_review_source_requires_confirmation_and_refuses_root(self):
        csrf = self.client.get("/api/csrf").get_json()["token"]
        created = self.client.post(
            "/api/review/batches",
            json={"paths": [str(self.album)]},
            headers={"X-CSRF-Token": csrf},
        ).get_json()
        item_id = created["items"][0]["id"]
        claimed = self.module.review_repository.claim_next()
        self.module.review_repository.complete_identification(
            claimed["queue_id"], claimed["item_id"], signature="v1",
            audio_count=1, current_artist="", current_album="",
            recommendation="none", candidates=[],
        )
        rejected = self.client.post(
            f"/api/review/items/{item_id}/recycle-source",
            json={"confirm_path": "wrong"},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(rejected.status_code, 409)
        self.assertTrue(self.album.is_dir())

        with self.module.review_repository._connection() as connection:
            connection.execute(
                "UPDATE review_items SET source_path = ? WHERE id = ?",
                (str(self.inbox), item_id),
            )
        root_rejected = self.client.post(
            f"/api/review/items/{item_id}/recycle-source",
            json={"confirm_path": str(self.inbox)},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(root_rejected.status_code, 409)
        self.assertIn("根目录", root_rejected.get_json()["error"])
        self.assertTrue(self.inbox.is_dir())

    def test_release_id_reidentify_endpoint_persists_request(self):
        csrf = self.client.get("/api/csrf").get_json()["token"]
        created = self.client.post(
            "/api/review/batches",
            json={"paths": [str(self.album)]},
            headers={"X-CSRF-Token": csrf},
        ).get_json()
        item_id = created["items"][0]["id"]
        claimed = self.module.review_repository.claim_next()
        self.module.review_repository.complete_identification(
            claimed["queue_id"],
            claimed["item_id"],
            signature="v1",
            audio_count=1,
            current_artist="艺术家",
            current_album="专辑",
            recommendation="none",
            candidates=[],
        )
        response = self.client.post(
            f"/api/review/items/{item_id}/identify",
            json={"release_id": "release-id"},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(response.status_code, 202)
        queued = self.module.review_repository.claim_next()
        self.assertEqual(
            json.loads(queued["payload_json"])["release_id"], "release-id"
        )

    def test_audio_preview_supports_range_and_rejects_traversal(self):
        csrf = self.client.get("/api/csrf").get_json()["token"]
        created = self.client.post(
            "/api/review/batches",
            json={"paths": [str(self.album)]},
            headers={"X-CSRF-Token": csrf},
        ).get_json()
        item_id = created["items"][0]["id"]
        response = self.client.get(
            f"/api/review/items/{item_id}/audio",
            query_string={"path": "01 第一首.flac"},
            headers={"Range": "bytes=0-2"},
        )
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.data, b"aud")
        rejected = self.client.get(
            f"/api/review/items/{item_id}/audio",
            query_string={"path": "../cover.jpg"},
        )
        self.assertEqual(rejected.status_code, 400)

    def test_direct_library_browse_sidecar_trash_and_restore(self):
        listing = self.client.get("/api/library/tracks").get_json()
        self.assertEqual(listing["total"], 1)
        self.assertEqual(listing["tracks"][0]["path"], "existing.flac")
        detail = self.client.get(
            "/api/library/track", query_string={"path": "existing.flac"}
        )
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.headers["Cache-Control"], "no-store")
        audio = self.client.get(
            "/api/library/audio",
            query_string={"path": "existing.flac"},
            headers={"Range": "bytes=0-6"},
        )
        self.assertEqual(audio.status_code, 206)
        self.assertEqual(audio.data, b"library")
        audio.close()

        csrf = self.client.get("/api/csrf").get_json()["token"]
        lyrics = self.client.post(
            "/api/library/lyrics/save",
            json={
                "path": "existing.flac",
                "mode": "sidecar",
                "content": "[00:01.00]line",
            },
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(lyrics.status_code, 200)
        self.assertTrue((self.library / "existing.lrc").is_file())
        reopened = self.client.get(
            "/api/library/track", query_string={"path": "existing.flac"}
        ).get_json()
        self.assertEqual(reopened["lyrics"]["sidecar"]["content"], "[00:01.00]line")

        trashed = self.client.post(
            "/api/library/trash",
            json={"path": "existing.flac"},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(trashed.status_code, 200)
        token = trashed.get_json()["token"]
        self.assertFalse((self.library / "existing.flac").exists())
        restored = self.client.post(
            "/api/library/trash/restore",
            json={"token": token},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(restored.status_code, 200)
        self.assertTrue((self.library / "existing.flac").is_file())

    def test_direct_library_folder_browse_trash_and_restore(self):
        album = self.library / "Artist" / "Album"
        album.mkdir(parents=True)
        (album / "song.flac").write_bytes(b"song")
        (album / "cover.jpg").write_bytes(b"cover")

        listing = self.client.get("/api/library/folders").get_json()
        folder = next(
            value for value in listing["folders"] if value["path"] == "Artist/Album"
        )
        self.assertEqual(folder["track_count"], 1)

        csrf = self.client.get("/api/csrf").get_json()["token"]
        trashed = self.client.post(
            "/api/library/trash/folder",
            json={"path": "Artist/Album"},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(trashed.status_code, 200)
        self.assertEqual(trashed.get_json()["kind"], "folder")
        self.assertFalse(album.exists())

        restored = self.client.post(
            "/api/library/trash/restore",
            json={"token": trashed.get_json()["token"]},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(restored.status_code, 200)
        self.assertTrue((album / "song.flac").is_file())
        self.assertTrue((album / "cover.jpg").is_file())

    def test_manual_filename_preview_and_approval_queue(self):
        csrf = self.client.get("/api/csrf").get_json()["token"]
        created = self.client.post(
            "/api/review/batches",
            json={"paths": [str(self.album)]},
            headers={"X-CSRF-Token": csrf},
        ).get_json()
        item_id = created["items"][0]["id"]
        claimed = self.module.review_repository.claim_next()
        self.module.review_repository.complete_identification(
            claimed["queue_id"],
            claimed["item_id"],
            signature="v1",
            audio_count=1,
            current_artist="艺术家",
            current_album="专辑",
            recommendation="none",
            candidates=[],
        )
        preview = self.client.get(
            f"/api/review/items/{item_id}/manual-preview"
        ).get_json()
        self.assertEqual(preview["data_source"], "manual")
        self.assertTrue(
            preview["tracks"][0]["target_path"].startswith(
                "未分类/艺术家/专辑/"
            )
        )
        response = self.client.post(
            f"/api/review/items/{item_id}/approve-manual",
            json={
                "albumartist": "手工艺术家",
                "album": "手工专辑",
                "tracks": [
                    {
                        "local_path": "01 第一首.flac",
                        "artist": "手工艺术家",
                        "title": "第一首",
                        "disc": 1,
                        "track": 1,
                    }
                ],
            },
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(response.status_code, 202)
        job = self.module.review_repository.claim_next("import")
        candidate = next(
            value
            for value in json.loads(job["candidates_json"])
            if value["key"] == job["selected_candidate_key"]
        )
        self.assertEqual(candidate["data_source"], "manual")
        self.assertTrue(
            candidate["tracks"][0]["target_path"].startswith(
                "未分类/手工艺术家/手工专辑/"
            )
        )

    def test_manual_lyric_search_and_selection_are_persisted(self):
        csrf = self.client.get("/api/csrf").get_json()["token"]
        created = self.client.post(
            "/api/review/batches",
            json={"paths": [str(self.album)]},
            headers={"X-CSRF-Token": csrf},
        ).get_json()
        item_id = created["items"][0]["id"]
        claimed = self.module.review_repository.claim_next()
        self.module.review_repository.complete_identification(
            claimed["queue_id"],
            claimed["item_id"],
            signature="v1",
            audio_count=1,
            current_artist="艺术家",
            current_album="专辑",
            recommendation="none",
            candidates=[],
        )

        service = mock.Mock()
        service.search.return_value = {
            "candidates": [
                {
                    "source": "kugou",
                    "provider_id": "123",
                    "title": "第一首",
                    "artist": "艺术家",
                    "score": 1,
                }
            ],
            "warnings": [],
        }
        service.fetch.return_value = {
            "source": "kugou",
            "provider_id": "123",
            "title": "第一首",
            "artist": "艺术家",
            "album": "专辑",
            "content": "[00:01.00]歌词",
            "synced": True,
            "digest": "digest",
        }
        with mock.patch(
            "music_organizer.review_routes.LyricsSearchService",
            return_value=service,
        ):
            searched = self.client.post(
                f"/api/review/items/{item_id}/lyrics/search",
                json={
                    "local_path": "01 第一首.flac",
                    "title": "第一首",
                    "artist": "艺术家",
                    "sources": ["kugou"],
                },
                headers={"X-CSRF-Token": csrf},
            )
            self.assertEqual(searched.status_code, 200)
            fetched = self.client.post(
                f"/api/review/items/{item_id}/lyrics/fetch",
                json={
                    "local_path": "01 第一首.flac",
                    "candidate": searched.get_json()["candidates"][0],
                },
                headers={"X-CSRF-Token": csrf},
            )
            self.assertEqual(fetched.status_code, 200)

        saved = self.client.post(
            f"/api/review/items/{item_id}/lyrics/save",
            json={
                "local_path": "01 第一首.flac",
                "decision": fetched.get_json() | {"status": "selected"},
            },
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(
            saved.get_json()["lyrics"]["01 第一首.flac"]["source"], "kugou"
        )

    def test_review_decision_is_persisted_and_completed_item_is_archived(self):
        csrf = self.client.get("/api/csrf").get_json()["token"]
        batch = self.client.post(
            "/api/review/batches",
            json={"paths": [str(self.album)]},
            headers={"X-CSRF-Token": csrf},
        ).get_json()
        item_id = batch["items"][0]["id"]
        claimed = self.module.review_repository.claim_next()
        self.module.review_repository.complete_identification(
            claimed["queue_id"],
            claimed["item_id"],
            signature="v1",
            audio_count=1,
            current_artist="艺术家",
            current_album="专辑",
            recommendation="strong",
            candidates=[
                {
                    "key": "musicbrainz:release-id",
                    "album_id": "release-id",
                    "tracks": [
                        {"local_path": "01 第一首.flac", "track_key": "track-id"}
                    ],
                    "track_options": [{"key": "track-id"}],
                    "auxiliary_files": ["cover.jpg"],
                }
            ],
        )
        approved = self.client.post(
            f"/api/review/items/{item_id}/approve",
            json={
                "candidate_key": "musicbrainz:release-id",
                "track_mapping": [
                    {"local_path": "01 第一首.flac", "track_key": "track-id"}
                ],
                "quarantine_paths": ["cover.jpg"],
            },
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(approved.status_code, 202)
        self.assertEqual(approved.get_json()["decision"]["quarantine_paths"], ["cover.jpg"])
        import_job = self.module.review_repository.claim_next("import")
        checkpoint = {
            "album_id": "release-id",
            "imported_track_count": 1,
            "imported_tracks": [
                {"source": "01 第一首.flac", "destination": "/library/01.flac"}
            ],
        }
        self.module.review_repository.checkpoint_import(
            import_job["queue_id"], import_job["item_id"], checkpoint
        )
        self.module.review_repository.complete_import(
            import_job["queue_id"],
            import_job["item_id"],
            checkpoint,
        )

        active = self.client.get("/api/review/batches?scope=active").get_json()
        archived = self.client.get("/api/review/batches?scope=archived").get_json()
        self.assertEqual(active["batches"], [])
        self.assertEqual(active["counts"], {"active": 0, "archived": 1})
        self.assertEqual(archived["batches"][0]["id"], batch["id"])
        archived_batch = self.client.get(
            f"/api/review/batches/{batch['id']}?scope=archived"
        ).get_json()
        self.assertTrue(archived_batch["items"][0]["archived"])
        searched = self.client.get(
            "/api/review/batches",
            query_string={"scope": "archived", "q": "艺术家"},
        ).get_json()
        self.assertEqual(searched["batches"][0]["id"], batch["id"])

        deleted = self.client.delete(
            f"/api/review/items/{item_id}/archive",
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertTrue(deleted.get_json()["batch_deleted"])
        self.assertEqual(
            self.client.get("/api/review/batches?scope=archived").get_json()[
                "counts"
            ]["archived"],
            0,
        )

    def test_history_and_config_are_served_by_the_vue_workspace(self):
        for path in ("/history", "/config"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            page = response.get_data(as_text=True)
            self.assertIn('<div id="app"></div>', page)
            self.assertNotIn('class="admin-sidebar"', page)

        history = self.client.get("/api/history").get_json()
        self.assertIn("items", history)
        self.assertIn("total", history)


if __name__ == "__main__":
    unittest.main()
