"""Persistent worker for batch MusicBrainz candidate identification."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sqlite3
import stat
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from music_organizer.beets_review import BeetsReviewMatcher, configure_http_proxy
from music_organizer.config import normalize_review_source_profiles
from music_organizer.lyrics import embed_imported_lyrics
from music_organizer.repository import SQLiteOrganizerRepository
from music_organizer.review import (
    AUDIO_EXTENSIONS,
    ReviewRepository,
    audio_files,
    finalize_review_import,
    quarantine_files,
    source_signature,
)
from music_organizer.runtime import runtime_is_ready
from music_organizer.source_guard import (
    SourceIdentity,
    parse_source_guard,
    snapshot_has_new_or_replaced_paths,
)
from music_organizer.source_guard import (
    serialize_source_guard as source_guard,
)
from music_organizer.source_guard import (
    source_identity_snapshot as capture_source_identity_snapshot,
)
from organizer import MusicOrganizer

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "/app/config/config.yaml"))
DATABASE_PATH = Path(
    os.environ.get("DATABASE_PATH", "/app/data/organizer.sqlite3")
)
LOG_PATH = Path(os.environ.get("LOG_PATH", "/app/data/organizer.log"))
REVIEW_HEARTBEAT_WRITE_SECONDS = max(
    1.0,
    min(
        float(os.environ.get("REVIEW_WORKER_HEARTBEAT_MAX_AGE_SECONDS", "30"))
        / 3,
        10.0,
    ),
)


@dataclass(frozen=True)
class SourceObservation:
    root: Path
    root_identity: tuple[int, int]
    snapshot: dict[str, SourceIdentity]


def source_identity_snapshot(root: Path) -> dict[str, SourceIdentity]:
    return capture_source_identity_snapshot(
        root,
        symlink_policy="record_and_skip",
    )


def observe_source(source_path: Path) -> SourceObservation | None:
    """Resolve and snapshot an import source without accepting symlink roots."""
    if source_path.is_symlink():
        return None
    try:
        root = source_path.resolve(strict=True)
        if not root.is_dir():
            return None
        metadata = root.stat()
        return SourceObservation(
            root=root,
            root_identity=(int(metadata.st_dev), int(metadata.st_ino)),
            snapshot=source_identity_snapshot(root),
        )
    except (FileNotFoundError, OSError, RuntimeError):
        return None


def _source_unchanged(
    expected_root_identity: tuple[int, int] | None,
    expected_snapshot: dict[str, SourceIdentity],
    observation: SourceObservation | None,
    *,
    allowed_metadata_changes: set[str] | None = None,
) -> bool:
    """Compare a fresh observation with the persisted approval boundary."""
    return bool(
        expected_root_identity is not None
        and observation is not None
        and observation.root_identity == expected_root_identity
        and not snapshot_has_new_or_replaced_paths(
            expected_snapshot,
            observation.snapshot,
            allowed_metadata_changes=allowed_metadata_changes,
        )
    )


def expected_hardlink_metadata_changes(
    root: Path,
    imported_tracks: list[dict],
    review_config: dict,
) -> set[str]:
    """Allow tag writes only when source and imported target are one inode."""
    if not (
        str(review_config.get("import_mode") or "hardlink").lower() == "hardlink"
        and bool(review_config.get("write_tags", False))
    ):
        return set()
    allowed: set[str] = set()
    for entry in imported_tracks:
        relative = PurePosixPath(str(entry.get("source") or ""))
        if not relative.parts or relative.is_absolute() or ".." in relative.parts:
            continue
        source_file = root.joinpath(*relative.parts)
        destination = Path(str(entry.get("destination") or ""))
        try:
            if source_file.is_file() and destination.is_file() and os.path.samefile(
                source_file, destination
            ):
                allowed.add(relative.as_posix())
        except OSError:
            continue
    return allowed


def _visible_directories(path: Path) -> list[Path]:
    return sorted(
        (
            child
            for child in path.iterdir()
            if child.is_dir()
            and not child.is_symlink()
            and not child.name.startswith(".music-organizer-")
            and child.name != "#recycle"
        ),
        key=lambda child: child.name.casefold(),
    )


def _has_direct_audio(path: Path) -> bool:
    try:
        return any(
            child.is_file() and child.suffix.lower() in AUDIO_EXTENSIONS
            for child in path.iterdir()
        )
    except OSError:
        return False


def discovery_directories(root: Path, mode: str) -> list[Path]:
    """Find album roots without splitting multi-disc album subdirectories."""
    children = _visible_directories(root)
    if mode != "artist_album":
        return children
    albums: list[Path] = []
    for artist in children:
        if _has_direct_audio(artist):
            albums.append(artist)
            continue
        albums.extend(_visible_directories(artist))
    return albums


class ReviewWorker:
    def __init__(self) -> None:
        self.shutdown_requested = threading.Event()
        self._next_heartbeat_at = 0.0
        self.import_lock = threading.Lock()
        self.organizer = MusicOrganizer(
            str(CONFIG_PATH),
            str(DATABASE_PATH),
            str(LOG_PATH),
            cancel_check=self.shutdown_requested.is_set,
        )
        self.repository = ReviewRepository(DATABASE_PATH)
        self.repository.initialize()
        self.repository.recover_interrupted()
        self.organizer.repository.set_app_state_value("review_import_active_at", "")
        config = self.organizer.load_config()
        self.discovery_observations: dict[str, tuple[str, float]] = {}
        self.discovery_settled: dict[
            str, tuple[tuple[int, int, int, int], str]
        ] = {}
        self.next_discovery_at = 0.0
        self.next_full_discovery_at = 0.0
        self._apply_runtime_config(config)
        revision = self._read_runtime_config_revision()
        self._runtime_config_revision = revision
        self._runtime_config_attempt_revision = revision

    @staticmethod
    def _read_runtime_config_revision() -> tuple[int, int, int, int, str] | None:
        try:
            metadata = CONFIG_PATH.stat()
            digest = hashlib.sha256(CONFIG_PATH.read_bytes()).hexdigest()
        except OSError:
            return None
        return (
            int(metadata.st_dev),
            int(metadata.st_ino),
            int(metadata.st_size),
            int(metadata.st_mtime_ns),
            digest,
        )

    def _apply_runtime_config(self, config: dict) -> None:
        review = config.get("review", {})
        if not isinstance(review, dict):
            raise ValueError("review 配置必须是对象")
        review = dict(review)
        source_profiles = normalize_review_source_profiles(review)
        review["source_profiles"] = source_profiles
        review["source_roots"] = [profile["path"] for profile in source_profiles]
        enabled = bool(review.get("enabled", False))
        worker_count = min(
            max(int(review.get("identify_workers", 3) or 3), 1), 8
        )
        poll_seconds = max(float(review.get("poll_seconds", 1) or 1), 0.2)
        max_attempts = min(
            max(int(review.get("max_attempts", 3) or 3), 1), 10
        )
        configured_import_timeout = str(
            os.environ.get("REVIEW_IMPORT_TIMEOUT_SECONDS", "") or ""
        ).strip() or review.get("import_timeout_seconds", 3600)
        import_timeout_seconds = min(
            max(float(configured_import_timeout or 3600), 60),
            86400,
        )
        auto_discover = any(
            profile["auto_discover"] for profile in source_profiles
        )
        discovery_interval_seconds = min(
            max(float(review.get("discovery_interval_seconds", 60) or 60), 30),
            3600,
        )
        discovery_full_rescan_seconds = min(
            max(
                float(review.get("discovery_full_rescan_seconds", 600) or 600),
                discovery_interval_seconds,
            ),
            86400,
        )
        discovery_stable_seconds = min(
            max(float(review.get("discovery_stable_seconds", 60) or 60), 10),
            86400,
        )
        import_config = dict(review)
        import_config["config_path"] = str(
            review.get("config_path")
            or DATABASE_PATH.parent / "review-beets-config.yaml"
        )
        beets_config_path = self.organizer.write_beets_config(import_config)
        profile_beets_config_paths: dict[str, Path] = {}
        base_import_mode = str(import_config.get("import_mode") or "hardlink").lower()
        for profile in source_profiles:
            effective = dict(import_config)
            effective["import_mode"] = profile["import_mode"]
            effective["move_extra_files"] = profile["move_extra_files"]
            effective["cleanup_source_after_import"] = profile[
                "cleanup_source_after_import"
            ]
            if profile["import_mode"] == base_import_mode:
                profile_beets_config_paths[profile["id"]] = beets_config_path
                continue
            suffix = beets_config_path.suffix or ".yaml"
            effective["config_path"] = str(
                beets_config_path.with_name(
                    f"{beets_config_path.stem}-{profile['id']}{suffix}"
                )
            )
            profile_beets_config_paths[profile["id"]] = (
                self.organizer.write_beets_config(effective)
            )
        matcher = BeetsReviewMatcher(beets_config_path)
        if enabled:
            matcher.configure()

        proxy_url = str(review.get("proxy_url") or "")
        configure_http_proxy(
            proxy_url,
            str(review.get("proxy_username") or ""),
            str(review.get("proxy_password") or ""),
        )

        self.review_config = review
        self.source_profiles = source_profiles
        self.profile_beets_config_paths = profile_beets_config_paths
        self.enabled = enabled
        self.worker_count = worker_count
        self.poll_seconds = poll_seconds
        self.max_attempts = max_attempts
        self.import_timeout_seconds = import_timeout_seconds
        self.auto_discover = auto_discover
        self.discovery_interval_seconds = discovery_interval_seconds
        self.discovery_full_rescan_seconds = discovery_full_rescan_seconds
        self.discovery_stable_seconds = discovery_stable_seconds
        self.import_config = import_config
        self.beets_config_path = beets_config_path
        self.matcher = matcher
        self.discovery_observations = {}
        self.discovery_settled = {}
        self.next_discovery_at = 0.0
        self.next_full_discovery_at = 0.0
        self.organizer.repository.set_app_state_value(
            "review_import_timeout_seconds",
            str(self.import_timeout_seconds),
        )

    def effective_review_config(self, source_path: Path) -> tuple[dict, Path]:
        """Resolve the most specific directory workflow for one review item."""
        resolved = source_path.expanduser().resolve(strict=False)
        matches: list[tuple[int, dict]] = []
        source_profiles = getattr(self, "source_profiles", None)
        if source_profiles is None:
            source_profiles = normalize_review_source_profiles(self.review_config)
        for profile in source_profiles:
            root = Path(profile["path"]).expanduser().resolve(strict=False)
            if resolved == root or resolved.is_relative_to(root):
                matches.append((len(root.parts), profile))
        effective = dict(self.review_config)
        config_path = self.beets_config_path
        if matches:
            profile = max(matches, key=lambda value: value[0])[1]
            for key in (
                "import_mode",
                "move_extra_files",
                "cleanup_source_after_import",
            ):
                effective[key] = profile[key]
            config_path = getattr(self, "profile_beets_config_paths", {}).get(
                profile["id"], self.beets_config_path
            )
        return effective, config_path

    def runtime_config_changed(self) -> bool:
        return (
            self._read_runtime_config_revision()
            != self._runtime_config_attempt_revision
        )

    def reload_runtime_config(self) -> bool:
        revision = self._read_runtime_config_revision()
        try:
            config = self.organizer.load_config()
            self._apply_runtime_config(config)
        except Exception as exc:
            self._runtime_config_attempt_revision = revision
            self.organizer.logger.error(
                "Review worker config reload failed; keeping last good config: %s",
                exc,
            )
            return False
        self._runtime_config_revision = revision
        self._runtime_config_attempt_revision = revision
        self.organizer.logger.info("Review worker configuration reloaded")
        return True

    def heartbeat(self) -> None:
        now = time.monotonic()
        if now < self._next_heartbeat_at:
            return
        self.organizer.repository.set_app_state_value(
            "review_worker_heartbeat",
            datetime.now().isoformat(timespec="seconds"),
        )
        self._next_heartbeat_at = now + REVIEW_HEARTBEAT_WRITE_SECONDS

    def discover_new_music(self, now: float | None = None) -> dict | None:
        """Queue album directories after their audio contents remain stable."""
        observed_at = time.monotonic() if now is None else now
        full_rescan_seconds = float(
            getattr(self, "discovery_full_rescan_seconds", 600)
        )
        full_rescan = observed_at >= getattr(
            self, "next_full_discovery_at", 0.0
        )
        if full_rescan:
            self.next_full_discovery_at = observed_at + full_rescan_seconds
        settled = getattr(self, "discovery_settled", {})
        current: dict[str, tuple[str, float]] = {}
        revisions: dict[str, tuple[int, int, int, int]] = {}
        stable_entries: list[tuple[Path, str]] = []
        source_profiles = getattr(self, "source_profiles", None)
        if source_profiles is None:
            source_profiles = normalize_review_source_profiles(self.review_config)
        for profile in source_profiles:
            if not profile.get("auto_discover"):
                continue
            configured_root = profile["path"]
            try:
                root = Path(configured_root).expanduser().resolve(strict=True)
                children = discovery_directories(
                    root, str(profile.get("discovery_mode") or "direct")
                )
            except OSError as exc:
                self.organizer.logger.warning(
                    "Review discovery skipped unavailable root %s: %s",
                    configured_root,
                    exc,
                )
                continue
            for child in children:
                if child.name.startswith(".music-organizer-") or child.name == "#recycle":
                    continue
                try:
                    metadata = child.stat(follow_symlinks=False)
                    if not stat.S_ISDIR(metadata.st_mode):
                        continue
                    key = str(child.resolve(strict=True))
                    revision = (
                        int(metadata.st_dev),
                        int(metadata.st_ino),
                        int(metadata.st_mtime_ns),
                        int(metadata.st_ctime_ns),
                    )
                    revisions[key] = revision
                    settled_entry = settled.get(key)
                    if (
                        not full_rescan
                        and settled_entry is not None
                        and settled_entry[0] == revision
                    ):
                        current[key] = (
                            settled_entry[1],
                            observed_at - self.discovery_stable_seconds,
                        )
                        continue
                    files = audio_files(child)
                    if not files:
                        continue
                    signature = source_signature(child, files)
                except OSError as exc:
                    self.organizer.logger.info(
                        "Review discovery deferred changing directory %s: %s",
                        child,
                        exc,
                    )
                    continue
                previous = self.discovery_observations.get(key)
                first_seen = (
                    previous[1]
                    if previous is not None and previous[0] == signature
                    else observed_at
                )
                current[key] = (signature, first_seen)
                if observed_at - first_seen >= self.discovery_stable_seconds:
                    stable_entries.append((Path(key), signature))
        self.discovery_observations = current
        batch = self.repository.create_discovered_batch(stable_entries)
        for path, signature in stable_entries:
            key = str(path)
            revision = revisions.get(key)
            if revision is not None:
                settled[key] = (revision, signature)
        self.discovery_settled = {
            key: value for key, value in settled.items() if key in current
        }
        if batch is not None:
            self.organizer.logger.info(
                "Automatically queued review batch %s with %s album(s)",
                batch["id"],
                len(batch.get("items", [])),
            )
        return batch

    def identify(self, job: dict) -> None:
        try:
            payload = json.loads(job.get("payload_json") or "{}")
            result = self.matcher.identify(
                job["source_path"],
                search_artist=payload.get("search_artist") or None,
                search_album=payload.get("search_album") or None,
                release_id=payload.get("release_id") or None,
            )
            self.repository.complete_identification(
                int(job["queue_id"]),
                int(job["item_id"]),
                **result,
            )
        except Exception as exc:
            self.organizer.logger.exception(
                "Review identification failed for %s: %s",
                job.get("source_path"),
                exc,
            )
            requeued = self.repository.fail(
                int(job["queue_id"]),
                int(job["item_id"]),
                str(exc),
                max_attempts=self.max_attempts,
            )
            if requeued:
                self.organizer.logger.warning(
                    "Review identification requeued after attempt %s/%s for %s",
                    int(job.get("attempts", 0)) + 1,
                    self.max_attempts,
                    job.get("source_path"),
                )

    def import_approved(self, job: dict) -> None:
        # All imports share one beets SQLite library. Identification remains
        # parallel, but library mutations must never overlap.
        with self.import_lock:
            self._import_approved(job)

    def _run_approved_import(
        self,
        *,
        job: dict,
        root: Path,
        candidate: dict,
        track_mapping: list,
        recovery_token: str,
        persisted_guard: dict,
        source_available: bool,
        source_changed: bool,
        hardlink_tag_recovery: bool,
        beets_config_path: Path,
    ) -> tuple[dict, bool]:
        manual_import = candidate.get("data_source") == "manual"
        if manual_import:
            command = [
                sys.executable,
                "-m",
                "music_organizer.manual_importer",
                "--config",
                str(beets_config_path),
                "--source",
                str(root),
                "--tracks-json",
                json.dumps(
                    candidate.get("tracks", []),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "--recovery-token",
                recovery_token,
            ]
        else:
            command = [
                sys.executable,
                "-m",
                "music_organizer.review_importer",
                "--config",
                str(beets_config_path),
                "--source",
                str(root),
                "--album-id",
                str(candidate["album_id"]),
                "--mapping-json",
                json.dumps(
                    track_mapping,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "--recovery-token",
                recovery_token,
            ]
        if persisted_guard and source_available:
            command.extend(
                [
                    "--import-guard-json",
                    json.dumps(
                        persisted_guard,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                ]
            )
        if not source_available or (
            source_changed and not hardlink_tag_recovery
        ):
            # Recovery is confined to beets items carrying this task's token.
            command.append("--recover-only")

        returncode, output = self.organizer.run_interruptible_process(
            command,
            timeout=self.import_timeout_seconds,
        )
        if returncode != 0:
            raise RuntimeError(output[-2000:] or f"beets 退出码 {returncode}")
        import_result: dict = {}
        for line in reversed(output.splitlines()):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and value.get("album_id"):
                import_result = value
                break
        if not import_result.get("album_id") or not import_result.get(
            "imported_tracks"
        ):
            raise RuntimeError("beets 成功退出但未返回完整入库清单")
        self.repository.checkpoint_import(
            int(job["queue_id"]),
            int(job["item_id"]),
            dict(import_result),
        )
        return import_result, bool(import_result.get("reused_existing_album"))

    def _import_approved(self, job: dict) -> None:
        self.organizer.repository.set_app_state_value(
            "review_import_active_at",
            datetime.now().isoformat(timespec="seconds"),
        )
        try:
            source_path = Path(job["source_path"])
            recovery_token = str(job.get("import_token") or "").strip()
            if not recovery_token:
                raise ValueError("入库任务缺少持久恢复令牌")
            checkpoint = json.loads(job.get("import_checkpoint_json") or "{}")
            recovery_completion = bool(checkpoint)
            persisted_guard = json.loads(job.get("import_guard_json") or "{}")
            candidates = json.loads(job["candidates_json"] or "[]")
            candidate = next(
                (
                    value for value in candidates
                    if value.get("key") == job["selected_candidate_key"]
                ),
                None,
            )
            if candidate is None or not candidate.get("album_id"):
                raise ValueError("确认的候选不存在，请重新识别")
            decision = json.loads(job.get("decision_json") or "{}")
            track_mapping = decision.get("track_mapping") or []
            manual_import = candidate.get("data_source") == "manual"
            effective_review_config, beets_config_path = (
                self.effective_review_config(source_path)
            )
            effective_tag_config = dict(effective_review_config)
            if manual_import:
                effective_tag_config["write_tags"] = True
            lyric_decisions = json.loads(job.get("lyrics_json") or "{}")
            root = source_path
            source_changed = False
            source_root_identity: tuple[int, int] | None = None
            source_snapshot: dict[str, SourceIdentity] = {}
            observation = observe_source(source_path)
            source_available = observation is not None
            if source_available:
                assert observation is not None
                root = observation.root
                current_root_identity = observation.root_identity
                current_snapshot = observation.snapshot
                if persisted_guard:
                    source_root_identity, source_snapshot = parse_source_guard(
                        persisted_guard
                    )
                    source_changed = (
                        current_root_identity != source_root_identity
                        or snapshot_has_new_or_replaced_paths(
                            source_snapshot,
                            current_snapshot,
                        )
                    )
                else:
                    source_changed = (
                        source_signature(root) != job["source_signature"]
                    )
                    if not source_changed:
                        source_root_identity = current_root_identity
                        source_snapshot = current_snapshot
                        if not recovery_completion:
                            persisted_guard = source_guard(
                                source_root_identity,
                                source_snapshot,
                            )
                            self.repository.checkpoint_import_guard(
                                int(job["queue_id"]),
                                int(job["item_id"]),
                                persisted_guard,
                            )

            hardlink_tag_recovery = bool(
                persisted_guard
                and source_available
                and source_changed
                and str(
                    effective_review_config.get("import_mode") or "hardlink"
                ).lower()
                == "hardlink"
                and bool(effective_tag_config.get("write_tags", False))
                and source_root_identity is not None
                and current_root_identity == source_root_identity
                and not snapshot_has_new_or_replaced_paths(
                    source_snapshot,
                    current_snapshot,
                    allowed_metadata_changes=set(source_snapshot),
                )
            )
            import_result = dict(checkpoint)
            if not import_result:
                import_result, reused_existing_album = self._run_approved_import(
                    job=job,
                    root=root,
                    candidate=candidate,
                    track_mapping=track_mapping,
                    recovery_token=recovery_token,
                    persisted_guard=persisted_guard,
                    source_available=source_available,
                    source_changed=source_changed,
                    hardlink_tag_recovery=hardlink_tag_recovery,
                    beets_config_path=beets_config_path,
                )
                recovery_completion = recovery_completion or reused_existing_album
            elif not import_result.get("album_id") or not import_result.get(
                "imported_tracks"
            ):
                raise RuntimeError("持久化入库检查点不完整")

            if lyric_decisions:
                lyric_results = embed_imported_lyrics(
                    list(import_result.get("imported_tracks", [])),
                    lyric_decisions,
                    self.review_config.get("directory", ""),
                )
                import_result["lyrics"] = lyric_results
                # Persist verified lyric writes before any destructive source
                # finalization. A retry may safely perform the same writes again.
                self.repository.checkpoint_import(
                    int(job["queue_id"]),
                    int(job["item_id"]),
                    dict(import_result),
                )

            # The subprocess may move the confirmed source or new audio may land
            # while beets is running. Re-check immediately before any destructive
            # finalization so a replacement file is never treated as the one the
            # user approved.
            allowed_metadata_changes: set[str] = set()
            if source_available:
                observation = observe_source(source_path)
                source_available = observation is not None
                if observation is not None:
                    root = observation.root
                    allowed_metadata_changes = expected_hardlink_metadata_changes(
                        root,
                        list(import_result.get("imported_tracks", [])),
                        effective_tag_config,
                    )
                    refreshed_changed = not _source_unchanged(
                        source_root_identity,
                        source_snapshot,
                        observation,
                        allowed_metadata_changes=allowed_metadata_changes,
                    )
                    if hardlink_tag_recovery and (
                        bool(checkpoint)
                        or bool(import_result.get("reused_existing_album"))
                    ):
                        # A prior checkpoint or the child has already established
                        # same-task ownership. Recompute instead of retaining the
                        # pre-run tag metadata delta, so the completed item stores
                        # its new stable signature.
                        source_changed = refreshed_changed
                    else:
                        source_changed = source_changed or refreshed_changed

            cleanup = []
            quarantine_paths = decision.get("quarantine_paths") or []
            if (
                quarantine_paths
                and source_available
                and not source_changed
                and not recovery_completion
            ):
                try:
                    cleanup = quarantine_files(
                        root,
                        self.review_config.get("source_roots", []),
                        quarantine_paths,
                        int(job["item_id"]),
                        expected_source_identities=source_snapshot,
                    )
                except Exception as exc:
                    cleanup = [
                        {
                            "source": "",
                            "status": "failed",
                            "error": str(exc),
                        }
                    ]
            failed_cleanup = [
                item for item in cleanup if item.get("status") == "failed"
            ]
            if failed_cleanup:
                self.organizer.logger.warning(
                    "Review import succeeded but %s cleanup action(s) failed for %s",
                    len(failed_cleanup),
                    root,
                )
            if source_available and not source_changed and not recovery_completion:
                observation = observe_source(source_path)
                source_available = observation is not None
                if observation is not None:
                    root = observation.root
                    source_changed = not _source_unchanged(
                        source_root_identity,
                        source_snapshot,
                        observation,
                        allowed_metadata_changes=allowed_metadata_changes,
                    )
            finalization = {}
            finalization_warnings = []
            manual_extra_files = (
                list(candidate.get("auxiliary_files") or [])
                if manual_import
                else []
            )
            recover_manual_extra_files = bool(
                recovery_completion and manual_extra_files
            )
            if source_changed:
                finalization_warnings.append(
                    "入库后源目录内容已变化；为避免删除新文件，已跳过隔离和源清理"
                )
            elif not source_available:
                finalization_warnings.append(
                    "源目录已由先前入库步骤移走；已从持久化检查点恢复任务"
                )
            elif recovery_completion:
                if recover_manual_extra_files:
                    finalization_warnings.append(
                        "任务由持久化入库结果恢复；仅继续处理确认时已记录的附属文件，"
                        "并跳过隔离和源音频清理"
                    )
                else:
                    finalization_warnings.append(
                        "任务由持久化入库结果恢复；为避免处理重启后同步的新文件，"
                        "已跳过隔离和源清理"
                    )
            should_move_extra_files = bool(
                effective_review_config.get("move_extra_files")
            ) or bool(manual_extra_files)
            should_finalize = should_move_extra_files or effective_review_config.get(
                "cleanup_source_after_import"
            )
            if (
                should_finalize
                and source_available
                and not source_changed
                and (not recovery_completion or recover_manual_extra_files)
            ):
                try:
                    finalization = finalize_review_import(
                        root,
                        self.review_config.get("source_roots", []),
                        self.review_config.get("directory", ""),
                        import_result.get("destination_directory", ""),
                        import_result.get("imported_tracks", []),
                        extra_file_patterns=(
                            []
                            if manual_import
                            else effective_review_config.get("extra_file_patterns", [])
                        ),
                        extra_file_paths=manual_extra_files,
                        flatten_extra_files=manual_import,
                        move_extra_files=should_move_extra_files,
                        cleanup_source_after_import=bool(
                            effective_review_config.get("cleanup_source_after_import")
                            and not recovery_completion
                        ),
                        expected_source_identities=source_snapshot,
                        allowed_source_metadata_changes=allowed_metadata_changes,
                    )
                    finalization_warnings.extend(finalization.get("warnings", []))
                    if root.is_dir():
                        observation = observe_source(source_path)
                        changed_during_finalization = not _source_unchanged(
                            source_root_identity,
                            source_snapshot,
                            observation,
                            allowed_metadata_changes=allowed_metadata_changes,
                        )
                        if changed_during_finalization:
                            source_changed = True
                            finalization_warnings.append(
                                "收尾期间检测到新建或替换文件；当前文件已保留，"
                                "并将交给后续自动发现处理"
                            )
                except Exception as exc:
                    finalization_warnings = [f"导入收尾失败: {exc}"]
                    self.organizer.logger.warning(
                        "Review import finalization failed for %s: %s", root, exc
                    )
            import_result.update(
                {
                    "candidate": {
                        key: candidate.get(key)
                        for key in (
                            "key",
                            "album_id",
                            "artist",
                            "album",
                            "year",
                            "country",
                        )
                    },
                    "cleanup": cleanup,
                    "warnings": [
                        item.get("error", "隔离文件失败")
                        for item in failed_cleanup
                    ] + finalization_warnings,
                    "additional_files": finalization.get("additional_files", []),
                    "source_cleanup": {
                        key: finalization.get(key)
                        for key in (
                            "removed_source_files",
                            "removed_directories",
                            "source_removed",
                            "remaining_files",
                        )
                        if key in finalization
                    },
                }
            )
            signature_after_import = ""
            try:
                if source_available and not source_changed and root.is_dir():
                    observation = observe_source(source_path)
                    changed_before_signature = not _source_unchanged(
                        source_root_identity,
                        source_snapshot,
                        observation,
                        allowed_metadata_changes=allowed_metadata_changes,
                    )
                    if not changed_before_signature:
                        assert observation is not None
                        candidate_signature = source_signature(root)
                        observation_after_signature = observe_source(source_path)
                        if (
                            observation_after_signature is not None
                            and observation_after_signature.root_identity
                            == observation.root_identity
                            and observation_after_signature.snapshot
                            == observation.snapshot
                        ):
                            signature_after_import = candidate_signature
                        else:
                            source_changed = True
                    else:
                        source_changed = True
                    if source_changed:
                        import_result.setdefault("warnings", []).append(
                            "完成记录前检测到源目录再次变化；未保存旧签名，"
                            "新文件将由后续自动发现处理"
                        )
            except (FileNotFoundError, OSError, RuntimeError) as exc:
                source_changed = True
                import_result.setdefault("warnings", []).append(
                    "完成记录前无法确认源目录稳定；未保存旧签名，"
                    "后续将重新扫描"
                )
                self.organizer.logger.info(
                    "Could not refresh post-import signature for %s: %s", root, exc
                )
            self.repository.complete_import(
                int(job["queue_id"]),
                int(job["item_id"]),
                import_result,
                source_signature_after_import=signature_after_import,
            )
        except Exception as exc:
            self.organizer.logger.exception(
                "Approved review import failed for %s: %s",
                job.get("source_path"),
                exc,
            )
            requeued = self.repository.fail(
                int(job["queue_id"]),
                int(job["item_id"]),
                str(exc),
                max_attempts=self.max_attempts,
            )
            if requeued:
                self.organizer.logger.warning(
                    "Approved review import requeued after attempt %s/%s for %s",
                    int(job.get("attempts", 0)) + 1,
                    self.max_attempts,
                    job.get("source_path"),
                )
        finally:
            self.organizer.repository.set_app_state_value(
                "review_import_active_at", ""
            )

    def run_once(self) -> bool:
        if self.runtime_config_changed():
            self.reload_runtime_config()
        if not self.enabled:
            self.heartbeat()
            return False
        job = self.repository.claim_next("identify")
        action = "identify"
        if job is None:
            job = self.repository.claim_next("import")
            action = "import"
        self.heartbeat()
        if job is None:
            return False
        (self.identify if action == "identify" else self.import_approved)(job)
        return True

    def request_shutdown(self, *_args) -> None:
        self.shutdown_requested.set()

    def run_forever(self) -> None:
        self.organizer.logger.info(
            "Review worker started with %s lookup slots", self.worker_count
        )
        futures: dict[Future, str] = {}
        reload_pending = False
        with ThreadPoolExecutor(
            max_workers=8, thread_name_prefix="review-lookup"
        ) as executor:
            while not self.shutdown_requested.is_set():
                self.heartbeat()
                reload_pending = (
                    reload_pending or self.runtime_config_changed()
                )
                if reload_pending and not futures:
                    self.reload_runtime_config()
                    reload_pending = False
                    if self.shutdown_requested.is_set():
                        break
                if self.enabled and not reload_pending:
                    now = time.monotonic()
                    if self.auto_discover and now >= self.next_discovery_at:
                        try:
                            self.discover_new_music(now)
                        except Exception:
                            self.organizer.logger.exception(
                                "Automatic review discovery failed"
                            )
                        finally:
                            self.next_discovery_at = (
                                now + self.discovery_interval_seconds
                            )
                    while len(futures) < self.worker_count:
                        job = self.repository.claim_next("identify")
                        if job is None and "import" not in futures.values():
                            job = self.repository.claim_next("import")
                        if job is None:
                            break
                        target = (
                            self.identify
                            if job["action"] == "identify"
                            else self.import_approved
                        )
                        futures[executor.submit(target, job)] = str(job["action"])
                if not futures:
                    self.shutdown_requested.wait(self.poll_seconds)
                    continue
                completed, _ = wait(
                    set(futures),
                    timeout=self.poll_seconds,
                    return_when=FIRST_COMPLETED,
                )
                for future in completed:
                    action = futures.pop(future, "unknown")
                    try:
                        future.result()
                    except Exception:
                        self.organizer.logger.exception(
                            "Review %s task crashed outside its job handler",
                            action,
                        )
        self.organizer.logger.info("Review worker stopped")


def heartbeat_is_fresh(
    max_age_seconds: int = 30,
    import_timeout_seconds: float | None = None,
) -> bool:
    repository = SQLiteOrganizerRepository(DATABASE_PATH)
    try:
        value = repository.app_state_value("review_worker_heartbeat", "")
        import_started = repository.app_state_value("review_import_active_at", "")
        stored_import_timeout = repository.app_state_value(
            "review_import_timeout_seconds", ""
        )
    except sqlite3.Error:
        return False
    if not value:
        return False
    try:
        heartbeat = datetime.fromisoformat(value)
    except ValueError:
        return False
    now = datetime.now()
    if (now - heartbeat).total_seconds() > max_age_seconds:
        return False
    if import_started:
        try:
            import_started_at = datetime.fromisoformat(import_started)
            configured_timeout = (
                import_timeout_seconds
                if import_timeout_seconds is not None
                else float(
                    stored_import_timeout
                    or os.environ.get("REVIEW_IMPORT_TIMEOUT_SECONDS", "")
                    or 3600
                )
            )
        except (TypeError, ValueError):
            return False
        if (now - import_started_at).total_seconds() > max(configured_timeout, 60) + 30:
            return False
    return True


def main() -> int:
    if "--health" in sys.argv:
        healthy = heartbeat_is_fresh() and runtime_is_ready(CONFIG_PATH, DATABASE_PATH)
        return 0 if healthy else 1
    worker = ReviewWorker()
    signal.signal(signal.SIGTERM, worker.request_shutdown)
    signal.signal(signal.SIGINT, worker.request_shutdown)
    if "--once" in sys.argv:
        worker.run_once()
    else:
        worker.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
