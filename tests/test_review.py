import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from music_organizer.review import (
    ReviewRepository,
    audio_files,
    ensure_within_roots,
    finalize_review_import,
    quarantine_files,
    source_signature,
)


class ReviewRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.inbox = self.root / "inbox"
        self.inbox.mkdir()
        self.album = self.inbox / "中文艺术家 - 中文专辑"
        self.album.mkdir()
        (self.album / "01 第一首.flac").write_bytes(b"audio-one")
        (self.album / "cover.jpg").write_bytes(b"image")
        self.repository = ReviewRepository(self.root / "organizer.sqlite3")
        self.repository.initialize()

    def tearDown(self):
        self.tempdir.cleanup()

    def test_review_paths_are_confined_to_configured_roots(self):
        self.assertEqual(
            ensure_within_roots(self.album, [self.inbox]),
            self.album.resolve(),
        )
        with self.assertRaisesRegex(ValueError, "不在允许范围"):
            ensure_within_roots(self.root, [self.inbox])

    def test_archives_can_be_searched_and_history_deleted(self):
        batch = self.repository.create_batch([self.album])
        item_id = batch["items"][0]["id"]
        with self.repository._connection() as conn:
            conn.execute(
                "UPDATE review_items SET current_artist = ?, current_album = ?, "
                "status = 'ready' "
                "WHERE id = ?",
                ("女王蜂", "星", item_id),
            )
        self.repository.skip(item_id)

        self.assertEqual(
            self.repository.batches(scope="archived", query="女王蜂")[0]["id"],
            batch["id"],
        )
        self.assertEqual(
            self.repository.batch(batch["id"], scope="archived", query="星")[
                "items"
            ][0]["id"],
            item_id,
        )
        self.assertEqual(
            self.repository.batches(scope="archived", query="不存在"), []
        )

        result = self.repository.delete_archived_item(item_id)

        self.assertTrue(result["batch_deleted"])
        self.assertEqual(self.repository.scope_counts()["archived"], 0)
        with self.assertRaisesRegex(KeyError, "不存在"):
            self.repository.item(item_id)

    def test_active_review_history_cannot_be_deleted(self):
        batch = self.repository.create_batch([self.album])

        with self.assertRaisesRegex(ValueError, "只能删除已归档"):
            self.repository.delete_archived_item(batch["items"][0]["id"])

    def test_signature_tracks_audio_changes_but_ignores_auxiliary_files(self):
        files = audio_files(self.album)
        self.assertEqual([path.name for path in files], ["01 第一首.flac"])
        first = source_signature(self.album, files)
        (self.album / "cover.jpg").write_bytes(b"changed image")
        self.assertEqual(source_signature(self.album), first)
        (self.album / "01 第一首.flac").write_bytes(b"changed audio")
        self.assertNotEqual(source_signature(self.album), first)

    def test_audio_files_rejects_file_and_directory_symlinks(self):
        outside = self.root / "outside"
        outside.mkdir()
        outside_audio = outside / "outside.flac"
        outside_audio.write_bytes(b"outside")
        inside_audio = next(self.album.glob("*.flac"))
        linked_file = self.album / "linked.flac"
        linked_inside = self.album / "linked-inside.flac"
        linked_dir = self.album / "linked-dir"
        try:
            linked_file.symlink_to(outside_audio)
            linked_inside.symlink_to(inside_audio)
            linked_dir.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink creation is unavailable: {exc}")

        self.assertEqual(len(audio_files(self.album)), 1)

    def test_audio_files_supports_dff_and_tta_but_not_late_cue(self):
        (self.album / "02.DFF").write_bytes(b"dsd")
        (self.album / "03.tta").write_bytes(b"tta")
        (self.album / "album.cue").write_text("late cue", encoding="utf-8")

        names = [path.name for path in audio_files(self.album)]

        self.assertEqual(names, ["01 第一首.flac", "02.DFF", "03.tta"])

    def test_discovery_queues_only_new_or_changed_album_signatures(self):
        first_signature = source_signature(self.album)
        batch = self.repository.create_discovered_batch(
            [(self.album, first_signature)]
        )
        self.assertIsNotNone(batch)
        self.assertIsNone(
            self.repository.create_discovered_batch([(self.album, first_signature)])
        )

        claimed = self.repository.claim_next()
        self.repository.complete_identification(
            claimed["queue_id"],
            claimed["item_id"],
            signature=first_signature,
            audio_count=1,
            current_artist="中文艺术家",
            current_album="中文专辑",
            recommendation="none",
            candidates=[],
        )
        self.repository.skip(claimed["item_id"])
        self.assertIsNone(
            self.repository.create_discovered_batch([(self.album, first_signature)])
        )

        (self.album / "02 第二首.flac").write_bytes(b"audio-two")
        changed_signature = source_signature(self.album)
        changed_batch = self.repository.create_discovered_batch(
            [(self.album, changed_signature)]
        )
        self.assertIsNotNone(changed_batch)
        self.assertEqual(changed_batch["items"][0]["source_signature"], changed_signature)

    def test_manual_batch_folds_selected_parent_and_child_directories(self):
        parent = self.inbox / "box-set"
        child = parent / "disc-1"
        child.mkdir(parents=True)
        (child / "01.flac").write_bytes(b"audio")

        batch = self.repository.create_batch([child, parent, child])

        self.assertEqual(len(batch["items"]), 1)
        self.assertEqual(batch["items"][0]["source_path"], str(parent.resolve()))

    def test_active_directory_overlap_is_rejected_but_archived_path_can_repeat(self):
        batch = self.repository.create_batch([self.album])
        with self.assertRaisesRegex(ValueError, "已有活跃预审任务"):
            self.repository.create_batch([self.album])
        with self.assertRaisesRegex(ValueError, "已有活跃预审任务"):
            self.repository.create_batch([self.inbox])

        claimed = self.repository.claim_next()
        self.repository.complete_identification(
            claimed["queue_id"],
            claimed["item_id"],
            signature="source-v1",
            audio_count=1,
            current_artist="",
            current_album="",
            recommendation="none",
            candidates=[],
        )
        self.repository.skip(batch["items"][0]["id"])

        repeated = self.repository.create_batch([self.album])
        self.assertNotEqual(repeated["id"], batch["id"])

    def test_batch_claim_and_candidate_results_are_persistent(self):
        batch = self.repository.create_batch([self.album], "首批测试")
        self.assertEqual(batch["status"], "queued")
        self.assertEqual(len(batch["items"]), 1)
        self.assertEqual(batch["items"][0]["candidates"], [])

        claimed = self.repository.claim_next()
        self.assertIsNotNone(claimed)
        self.assertIsNone(self.repository.claim_next())
        self.repository.complete_identification(
            claimed["queue_id"],
            claimed["item_id"],
            signature="source-v1",
            audio_count=1,
            current_artist="中文艺术家",
            current_album="中文专辑",
            recommendation="strong",
            candidates=[
                {
                    "key": "musicbrainz:release-id",
                    "album_id": "release-id",
                    "artist": "中文艺术家",
                    "album": "中文专辑",
                    "score": 0.98,
                    "tracks": [
                        {
                            "local_path": "01 第一首.flac",
                            "track_key": "track-id",
                        }
                    ],
                    "local_items": [
                        {
                            "local_path": "01 第一首.flac",
                            "track_key": "track-id",
                        }
                    ],
                    "track_options": [{"key": "track-id"}],
                    "auxiliary_files": ["cover.jpg"],
                }
            ],
        )

        stored = self.repository.batch(batch["id"])
        self.assertEqual(stored["status"], "needs_review")
        self.assertEqual(stored["items"][0]["status"], "ready")
        self.assertEqual(
            stored["items"][0]["candidates"][0]["key"],
            "musicbrainz:release-id",
        )

        reopened = ReviewRepository(self.root / "organizer.sqlite3")
        reopened.initialize()
        self.assertEqual(reopened.batch(batch["id"]), stored)

        approved = reopened.approve(
            stored["items"][0]["id"],
            "musicbrainz:release-id",
            track_mapping=[
                {"local_path": "01 第一首.flac", "track_key": "track-id"}
            ],
            quarantine_paths=["cover.jpg"],
        )
        self.assertEqual(approved["status"], "approved")
        import_job = reopened.claim_next("import")
        self.assertEqual(import_job["selected_candidate_key"], "musicbrainz:release-id")
        self.assertEqual(
            json.loads(import_job["decision_json"])["quarantine_paths"],
            ["cover.jpg"],
        )
        self.assertTrue(import_job["import_token"])
        guard = {
            "root": [11, 22],
            "entries": {
                "01 \u7b2c\u4e00\u9996.flac": [1, 2, 32768, 10, 20],
            },
        }
        reopened.checkpoint_import_guard(
            import_job["queue_id"], import_job["item_id"], guard
        )
        with self.assertRaisesRegex(ValueError, "检查点"):
            reopened.complete_import(
                import_job["queue_id"],
                import_job["item_id"],
                {"imported_track_count": 1},
            )
        checkpoint = {
            "album_id": "release-id",
            "imported_track_count": 1,
            "imported_tracks": [
                {
                    "source": "01 第一首.flac",
                    "destination": "/library/01 第一首.flac",
                }
            ],
            "destination_directory": "/library",
        }
        reopened.checkpoint_import(
            import_job["queue_id"], import_job["item_id"], checkpoint
        )
        reopened.recover_interrupted()
        recovered_job = reopened.claim_next("import")
        self.assertEqual(recovered_job["import_token"], import_job["import_token"])
        self.assertEqual(recovered_job["import_stage"], "beets_committed")
        self.assertEqual(json.loads(recovered_job["import_guard_json"]), guard)
        self.assertEqual(
            json.loads(recovered_job["import_checkpoint_json"]), checkpoint
        )
        reopened.complete_import(
            recovered_job["queue_id"],
            recovered_job["item_id"],
            checkpoint,
            source_signature_after_import="post-import-signature",
        )
        completed = reopened.item(import_job["item_id"])
        self.assertEqual(completed["status"], "done")
        self.assertEqual(completed["source_signature"], "post-import-signature")
        self.assertTrue(completed["archived"])
        self.assertEqual(reopened.batches(scope="active"), [])
        self.assertEqual(reopened.scope_counts(), {"active": 0, "archived": 1})
        archived = reopened.batch(batch["id"], scope="archived")
        self.assertEqual(archived["items"][0]["import_result"]["imported_track_count"], 1)

        with reopened._connection() as conn:
            conn.execute(
                "UPDATE review_items SET archived_at = '' WHERE id = ?",
                (import_job["item_id"],),
            )
        reopened.initialize()
        self.assertTrue(reopened.item(import_job["item_id"])["archived"])

    def test_decision_rejects_duplicate_tracks_and_cleanup_of_imported_file(self):
        candidate = {
            "tracks": [
                {"local_path": "01.flac", "track_key": "track-1"},
                {"local_path": "02.flac", "track_key": "track-2"},
            ],
            "track_options": [{"key": "track-1"}, {"key": "track-2"}],
        }
        with self.assertRaisesRegex(ValueError, "不能重复对应"):
            self.repository._validate_decision(
                candidate,
                [
                    {"local_path": "01.flac", "track_key": "track-1"},
                    {"local_path": "02.flac", "track_key": "track-1"},
                ],
                [],
            )
        with self.assertRaisesRegex(ValueError, "不能移入隔离区"):
            self.repository._validate_decision(
                candidate,
                [{"local_path": "01.flac", "track_key": "track-1"}],
                ["01.flac"],
            )

    def test_quarantine_moves_only_explicitly_selected_file(self):
        notes = self.album / "notes.txt"
        notes.write_text("keep", encoding="utf-8")
        result = quarantine_files(
            self.album,
            [self.inbox],
            ["cover.jpg"],
            42,
        )
        self.assertEqual(result[0]["status"], "quarantined")
        self.assertFalse((self.album / "cover.jpg").exists())
        self.assertTrue(notes.exists())
        destination = self.inbox / result[0]["destination"]
        self.assertTrue(destination.is_file())
        self.assertIn(".music-organizer-quarantine", destination.parts)
        recovered = quarantine_files(
            self.album,
            [self.inbox],
            ["cover.jpg"],
            42,
        )
        self.assertEqual(recovered, result)

    def test_quarantine_rejects_symlinked_hidden_root(self):
        outside = self.root / "outside-quarantine"
        outside.mkdir()
        hidden = self.inbox / ".music-organizer-quarantine"
        try:
            hidden.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink creation is unavailable: {exc}")

        with self.assertRaisesRegex(ValueError, "符号链接"):
            quarantine_files(
                self.album,
                [self.inbox],
                ["cover.jpg"],
                42,
            )

        self.assertTrue((self.album / "cover.jpg").is_file())
        self.assertEqual(list(outside.rglob("*")), [])

    def test_import_finalization_moves_images_case_insensitively_and_keeps_unknowns(self):
        target_root = self.root / "library"
        target_album = target_root / "中文艺术家" / "中文专辑"
        target_album.mkdir(parents=True)
        target_audio = target_album / "01 第一首.flac"
        target_audio.write_bytes(b"audio-one")
        scans = self.album / "Scans"
        scans.mkdir()
        (scans / "Back.PNG").write_bytes(b"back")
        (self.album / "notes.txt").write_text("keep", encoding="utf-8")

        result = finalize_review_import(
            self.album,
            [self.inbox],
            target_root,
            target_album,
            [
                {
                    "source": "01 第一首.flac",
                    "destination": str(target_audio),
                }
            ],
            extra_file_patterns=["*.jpg", "*.png"],
            move_extra_files=True,
            cleanup_source_after_import=True,
        )

        self.assertFalse((self.album / "01 第一首.flac").exists())
        self.assertFalse((self.album / "cover.jpg").exists())
        self.assertTrue((target_album / "cover.jpg").is_file())
        self.assertTrue((target_album / "Scans" / "Back.PNG").is_file())
        self.assertEqual(result["remaining_files"], ["notes.txt"])
        self.assertFalse(result["source_removed"])
        self.assertTrue(result["warnings"])

    def test_import_finalization_moves_only_explicit_manual_auxiliary_files(self):
        target_root = self.root / "library"
        target_album = target_root / "Artist" / "Tagged Album"
        target_album.mkdir(parents=True)
        target_audio = target_album / "01 第一首.flac"
        target_audio.write_bytes(b"audio-one")
        (self.album / "02 未选择.flac").write_bytes(b"unselected")
        scans = self.album / "Scans"
        scans.mkdir()
        (scans / "album.log").write_text("log", encoding="utf-8")

        result = finalize_review_import(
            self.album,
            [self.inbox],
            target_root,
            target_album,
            [
                {
                    "source": "01 第一首.flac",
                    "destination": str(target_audio),
                }
            ],
            extra_file_paths=["cover.jpg", "Scans/album.log"],
            flatten_extra_files=True,
            move_extra_files=True,
        )

        self.assertTrue((target_album / "cover.jpg").is_file())
        self.assertTrue((target_album / "album.log").is_file())
        self.assertFalse((self.album / "cover.jpg").exists())
        self.assertFalse((scans / "album.log").exists())
        self.assertTrue((self.album / "01 第一首.flac").is_file())
        self.assertTrue((self.album / "02 未选择.flac").is_file())
        self.assertEqual(
            {entry["source"] for entry in result["additional_files"]},
            {"cover.jpg", "Scans/album.log"},
        )

    def test_import_finalization_removes_source_when_every_file_is_handled(self):
        target_root = self.root / "library"
        target_album = target_root / "album"
        target_album.mkdir(parents=True)
        target_audio = target_album / "01.flac"
        target_audio.write_bytes(b"audio-one")

        result = finalize_review_import(
            self.album,
            [self.inbox],
            target_root,
            target_album,
            [
                {
                    "source": "01 第一首.flac",
                    "destination": str(target_audio),
                }
            ],
            extra_file_patterns=["*.JPG"],
            move_extra_files=True,
            cleanup_source_after_import=True,
        )

        self.assertTrue(result["source_removed"])
        self.assertFalse(self.album.exists())
        self.assertEqual(len(result["additional_files"]), 1)

    def test_import_finalization_preserves_extra_replaced_during_copy(self):
        target_root = self.root / "library"
        target_album = target_root / "album"
        target_album.mkdir(parents=True)
        target_audio = target_album / "01.flac"
        target_audio.write_bytes(b"audio-one")
        cover = self.album / "cover.jpg"
        real_copystat = shutil.copystat

        def replace_cover(source, target, *, follow_symlinks=True):
            real_copystat(source, target, follow_symlinks=follow_symlinks)
            replacement = self.album / ".new-cover"
            replacement.write_bytes(b"new synchronized cover")
            replacement.replace(cover)

        with mock.patch(
            "music_organizer.review.shutil.copystat",
            side_effect=replace_cover,
        ):
            result = finalize_review_import(
                self.album,
                [self.inbox],
                target_root,
                target_album,
                [
                    {
                        "source": "01 第一首.flac",
                        "destination": str(target_audio),
                    }
                ],
                extra_file_patterns=["*.jpg"],
                move_extra_files=True,
            )

        self.assertEqual(cover.read_bytes(), b"new synchronized cover")
        self.assertFalse((target_album / "cover.jpg").exists())
        self.assertIn("复制期间发生变化", " ".join(result["warnings"]))

    def test_import_finalization_preserves_audio_replaced_during_extra_copy(self):
        target_root = self.root / "library"
        target_album = target_root / "album"
        target_album.mkdir(parents=True)
        target_audio = target_album / "01.flac"
        target_audio.write_bytes(b"audio-one")
        source_audio = self.album / "01 第一首.flac"
        real_copystat = shutil.copystat

        def replace_audio(source, target, *, follow_symlinks=True):
            real_copystat(source, target, follow_symlinks=follow_symlinks)
            replacement = self.album / ".new-audio"
            replacement.write_bytes(b"new synchronized audio")
            replacement.replace(source_audio)

        with mock.patch(
            "music_organizer.review.shutil.copystat",
            side_effect=replace_audio,
        ):
            result = finalize_review_import(
                self.album,
                [self.inbox],
                target_root,
                target_album,
                [
                    {
                        "source": "01 第一首.flac",
                        "destination": str(target_audio),
                    }
                ],
                extra_file_patterns=["*.jpg"],
                move_extra_files=True,
                cleanup_source_after_import=True,
            )

        self.assertEqual(source_audio.read_bytes(), b"new synchronized audio")
        self.assertTrue((target_album / "cover.jpg").is_file())
        self.assertIn("入库音频在删除前发生变化", " ".join(result["warnings"]))

    def test_import_finalization_rejects_symlinked_destination_directory(self):
        target_root = self.root / "library"
        target_album = target_root / "album"
        target_album.mkdir(parents=True)
        target_audio = target_album / "01.flac"
        target_audio.write_bytes(b"audio-one")
        outside = self.root / "outside-library"
        outside.mkdir()
        scans = self.album / "Scans"
        scans.mkdir()
        source_cover = scans / "Back.PNG"
        source_cover.write_bytes(b"back")
        try:
            (target_album / "Scans").symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink creation is unavailable: {exc}")

        with self.assertRaisesRegex(ValueError, "符号链接"):
            finalize_review_import(
                self.album,
                [self.inbox],
                target_root,
                target_album,
                [
                    {
                        "source": "01 第一首.flac",
                        "destination": str(target_audio),
                    }
                ],
                extra_file_patterns=["*.png"],
                move_extra_files=True,
            )

        self.assertTrue(source_cover.is_file())
        self.assertFalse((outside / "Back.PNG").exists())

    def test_import_finalization_rejects_symlinked_source_cleanup_path(self):
        target_root = self.root / "library"
        target_album = target_root / "album"
        target_album.mkdir(parents=True)
        target_audio = target_album / "01.flac"
        target_audio.write_bytes(b"audio-one")
        outside = self.root / "outside-source"
        outside.mkdir()
        outside_audio = outside / "outside.flac"
        outside_audio.write_bytes(b"outside")
        linked = self.album / "linked"
        try:
            linked.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink creation is unavailable: {exc}")

        with self.assertRaisesRegex(ValueError, "符号链接"):
            finalize_review_import(
                self.album,
                [self.inbox],
                target_root,
                target_album,
                [
                    {
                        "source": "linked/outside.flac",
                        "destination": str(target_audio),
                    }
                ],
                cleanup_source_after_import=True,
            )

        self.assertTrue(outside_audio.is_file())

    def test_interrupted_identification_is_requeued(self):
        batch = self.repository.create_batch([self.album])
        claimed = self.repository.claim_next()
        self.assertEqual(
            self.repository.batch(batch["id"])["items"][0]["status"],
            "identifying",
        )

        self.repository.recover_interrupted()
        reclaimed = self.repository.claim_next()
        self.assertEqual(reclaimed["item_id"], claimed["item_id"])
        self.assertEqual(reclaimed["attempts"], 1)

    def test_failure_retries_three_attempts_before_terminal_failure(self):
        batch = self.repository.create_batch([self.album])

        for expected_attempt in range(1, 4):
            claimed = self.repository.claim_next()
            self.assertIsNotNone(claimed)
            requeued = self.repository.fail(
                claimed["queue_id"],
                claimed["item_id"],
                "temporary lookup failure",
                max_attempts=3,
            )
            self.assertEqual(requeued, expected_attempt < 3)

        item = self.repository.batch(batch["id"])["items"][0]
        self.assertEqual(item["status"], "failed")
        self.assertIsNone(self.repository.claim_next())

        with self.repository._connection() as conn:
            self.assertEqual(
                conn.execute("PRAGMA cache_size").fetchone()[0],
                -20000,
            )
            attempts = conn.execute(
                "SELECT attempts FROM review_queue WHERE item_id = ?",
                (item["id"],),
            ).fetchone()[0]
        self.assertEqual(attempts, 3)

    def test_reidentify_keeps_exact_release_request_in_persistent_queue(self):
        self.repository.create_batch([self.album])
        claimed = self.repository.claim_next()
        self.repository.complete_identification(
            claimed["queue_id"],
            claimed["item_id"],
            signature="v1",
            audio_count=1,
            current_artist="旧艺术家",
            current_album="旧专辑",
            recommendation="none",
            candidates=[],
        )
        self.repository.reidentify(
            claimed["item_id"], release_id="exact-release-id"
        )
        queued = self.repository.claim_next()
        self.assertEqual(
            json.loads(queued["payload_json"])["release_id"],
            "exact-release-id",
        )


if __name__ == "__main__":
    unittest.main()
