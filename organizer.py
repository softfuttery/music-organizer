import logging
import os
import shutil
import signal
import stat
import subprocess
import time
import urllib.error
from datetime import datetime
from fnmatch import fnmatchcase
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable

import yaml

from music_organizer.config import (
    DEFAULT_BEETS_PATH_FORMAT,
    load_config,
    migrate_plaintext_credentials,
    normalize_exts,
    save_config,
)
from music_organizer.cue import (
    CueProcessor,
    CueSplitOptions,
    normalized_cue_file_name,
    parse_cue,
    resolve_cue_audio,
)
from music_organizer.file_compare import stable_regular_files_equal
from music_organizer.logs import tail_lines
from music_organizer.models import CueSheet, CueTrack, RunResult
from music_organizer.pathsafe import (
    reject_symlink_components as reject_path_symlinks,
)
from music_organizer.pathsafe import resolve_confined, resolve_root
from music_organizer.qbittorrent import QBittorrentClient
from music_organizer.repository import OrganizerRepository, SQLiteOrganizerRepository


class MusicOrganizer:
    """Scans source folders and organizes music files into the target library."""

    def __init__(
        self,
        config_path: str,
        database_path: str,
        log_path: str,
        *,
        repository: OrganizerRepository | None = None,
        cue_processor: CueProcessor | None = None,
        cancel_check: Callable[[], bool] | None = None,
        file_logging: bool = True,
    ):
        self.config_path = Path(config_path)
        self.database_path = Path(database_path)
        self.log_path = Path(log_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        migrate_plaintext_credentials(self.config_path)
        self._cancel_check = cancel_check or (lambda: False)
        self.repository = repository or SQLiteOrganizerRepository(self.database_path)
        self.cue_processor = cue_processor or CueProcessor(self.stop_requested)
        self._cue_sheet_cache: dict[Path, CueSheet] = {}

        self.logger = logging.getLogger("music_organizer")
        log_level_name = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
        self.logger.setLevel(getattr(logging, log_level_name, logging.INFO))
        self.logger.propagate = False
        if not self.logger.handlers:
            formatter = logging.Formatter(
                "%(asctime)s [%(levelname)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
            if file_logging:
                file_handler = RotatingFileHandler(
                    self.log_path,
                    maxBytes=10 * 1024 * 1024,
                    backupCount=3,
                    encoding="utf-8",
                )
                file_handler.setFormatter(formatter)
                self.logger.addHandler(file_handler)
            stream_handler = logging.StreamHandler()
            stream_handler.setFormatter(formatter)
            self.logger.addHandler(stream_handler)
        self.repository.initialize()

    def stop_requested(self) -> bool:
        return bool(self._cancel_check())

    def run_interruptible_process(
        self,
        cmd: list[str],
        timeout: float | None = None,
    ) -> tuple[int, str]:
        popen_kwargs: dict[str, Any] = {}
        if os.name == "posix":
            # Isolate the command so cancellation also reaches helpers spawned by
            # beets (for example a decoder), instead of leaving an orphan behind.
            popen_kwargs["start_new_session"] = True
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            **popen_kwargs,
        )

        def stop_process(*, force: bool = False) -> None:
            if process.poll() is not None:
                return
            if os.name == "posix":
                try:
                    os.killpg(
                        process.pid,
                        signal.SIGKILL if force else signal.SIGTERM,
                    )
                    return
                except ProcessLookupError:
                    return
            (process.kill if force else process.terminate)()

        def stop_and_collect() -> str:
            stop_process()
            try:
                captured, _ = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                stop_process(force=True)
                captured, _ = process.communicate()
            return captured or ""

        started_at = time.monotonic()
        initial_wait = 0.2 if timeout is None else min(max(timeout, 0.0), 0.2)
        try:
            output, _ = process.communicate(timeout=initial_wait)
            return process.returncode, output or ""
        except subprocess.TimeoutExpired:
            pass

        while True:
            try:
                output, _ = process.communicate(timeout=0.2)
                return process.returncode, output or ""
            except subprocess.TimeoutExpired:
                if self.stop_requested():
                    stop_and_collect()
                    raise InterruptedError("stopped by user") from None
                if timeout is not None and time.monotonic() - started_at >= timeout:
                    stop_and_collect()
                    raise TimeoutError(f"Process timed out: {' '.join(cmd)}") from None

    def load_config(self) -> dict[str, Any]:
        return load_config(self.config_path)

    @staticmethod
    def normalize_exts(exts: list[Any]) -> set[str]:
        return normalize_exts(exts)

    def save_config(self, config: dict[str, Any]) -> None:
        save_config(self.config_path, config)
        self.logger.info("Config saved: %s", self.config_path)

    def init_db(self) -> None:
        self.repository.initialize()

    def already_processed(self, source_path: Path) -> bool:
        return self.repository.is_processed(source_path)

    def processed_sources(self) -> set[str]:
        return self.repository.processed_sources()

    def record_file(
        self,
        source_path: Path,
        target_path: Path,
        mode: str,
        status: str,
        message: str = "",
    ) -> None:
        self.repository.record_file(source_path, target_path, mode, status, message)

    def record_files(
        self,
        records: list[tuple[Path, Path, str, str, str]],
    ) -> None:
        batch_writer = getattr(self.repository, "record_files", None)
        if callable(batch_writer):
            batch_writer(records)
            return
        for source_path, target_path, mode, status, message in records:
            self.repository.record_file(
                source_path, target_path, mode, status, message
            )

    def create_run(self) -> int:
        return self.repository.create_run()

    def app_state_value(self, key: str, default: str = "") -> str:
        return self.repository.app_state_value(key, default)

    def set_app_state_value(self, key: str, value: str) -> None:
        self.repository.set_app_state_value(key, value)

    def seen_qb_hashes(self) -> set[str]:
        return self.repository.seen_qb_hashes()

    def record_qb_torrents(
        self,
        torrents: list[dict[str, Any]],
        status: str,
        message: str = "",
    ) -> None:
        self.repository.record_qb_torrents(torrents, status, message)

    def finish_run(self, run_id: int, result: RunResult) -> None:
        self.repository.finish_run(run_id, result)

    def update_run_progress(self, run_id: int, result: RunResult) -> None:
        self.repository.update_run_progress(run_id, result)
    def should_exclude(self, path: Path, source_root: Path, config: dict[str, Any]) -> bool:
        rel = path.relative_to(source_root).as_posix()
        ext = path.suffix.lower()
        excluded_exts = {str(item).lower() for item in config["exclude"].get("exts", [])}
        if ext in excluded_exts:
            return True

        rel_with_slashes = (f"{rel}/" if path.is_dir() else rel).casefold()
        for pattern in config["exclude"].get("globs", []):
            normalized_pattern = str(pattern).casefold()
            if fnmatchcase(rel_with_slashes, normalized_pattern) or fnmatchcase(
                f"/{rel_with_slashes}", normalized_pattern
            ):
                return True
        return False

    def should_include(self, path: Path, source_root: Path, config: dict[str, Any]) -> bool:
        include_globs = [item for item in config.get("include", {}).get("globs", []) if item]
        if not include_globs:
            return True

        rel = path.relative_to(source_root).as_posix().casefold()
        for pattern in include_globs:
            normalized_pattern = str(pattern).casefold()
            if fnmatchcase(rel, normalized_pattern) or fnmatchcase(
                f"/{rel}", normalized_pattern
            ):
                return True
        return False

    def has_included_extension(self, path: Path, config: dict[str, Any]) -> bool:
        include_exts = self.normalize_exts(config.get("include", {}).get("exts", []))
        return path.suffix.lower() in include_exts

    @staticmethod
    def path_relative_to(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False


    @classmethod
    def path_is_same_or_under(cls, path: Path, root: Path) -> bool:
        """Return whether path resolves to root or one of its descendants."""
        try:
            path.resolve(strict=False).relative_to(root.resolve(strict=False))
            return True
        except ValueError:
            return False

    def iter_candidate_files(
        self,
        scan_root: Path,
        config: dict[str, Any],
        rules_root: Path | None = None,
    ):
        rules_root = rules_root or scan_root
        if scan_root.is_symlink() or rules_root.is_symlink():
            return
        try:
            relative_scan_root = scan_root.relative_to(rules_root)
        except ValueError:
            return
        current_path = rules_root
        for part in relative_scan_root.parts:
            if part in {"", ".", ".."}:
                return
            current_path = current_path / part
            if current_path.is_symlink():
                return
        try:
            resolved_rules_root = rules_root.resolve(strict=True)
            resolved_scan_root = scan_root.resolve(strict=True)
        except (OSError, RuntimeError):
            return
        if not resolved_scan_root.is_relative_to(resolved_rules_root):
            return
        rules_root = resolved_rules_root
        scan_root = resolved_scan_root
        if scan_root.is_file():
            if self.should_exclude(scan_root, rules_root, config):
                return
            if not self.should_include(scan_root, rules_root, config):
                return
            if not self.has_included_extension(scan_root, config):
                return
            yield scan_root
            return

        for root, dirnames, filenames in os.walk(scan_root):
            root_path = Path(root)
            kept_dirs = []
            for dirname in dirnames:
                dir_path = root_path / dirname
                try:
                    metadata = dir_path.stat(follow_symlinks=False)
                    if not stat.S_ISDIR(metadata.st_mode):
                        continue
                    resolved_dir = dir_path.resolve(strict=True)
                except (OSError, RuntimeError):
                    continue
                if not resolved_dir.is_relative_to(rules_root):
                    continue
                if self.should_exclude(dir_path, rules_root, config):
                    self.logger.debug("Excluded directory: %s", dir_path)
                    continue
                kept_dirs.append(dirname)
            dirnames[:] = kept_dirs

            for filename in filenames:
                file_path = root_path / filename
                try:
                    metadata = file_path.stat(follow_symlinks=False)
                    if not stat.S_ISREG(metadata.st_mode):
                        continue
                    resolved_file = file_path.resolve(strict=True)
                except (OSError, RuntimeError):
                    continue
                if not resolved_file.is_relative_to(rules_root):
                    continue
                if self.should_exclude(file_path, rules_root, config):
                    self.logger.debug("Excluded file: %s", file_path)
                    continue
                if not self.should_include(file_path, rules_root, config):
                    self.logger.debug("Skipped by include rules: %s", file_path)
                    continue
                if not self.has_included_extension(file_path, config):
                    self.logger.debug("Skipped by extension rules: %s", file_path)
                    continue
                yield file_path

    def target_for(
        self,
        source_file: Path,
        source_root: Path,
        target_root: Path,
        config: dict[str, Any],
    ) -> Path:
        relative_file = source_file.relative_to(source_root)
        if config.get("keep_dir_struct", True):
            target_path = target_root / relative_file
        else:
            target_path = target_root / source_file.name

        if config.get("mkdir_if_single", True) and source_file.parent == source_root:
            target_path = target_root / source_file.stem / source_file.name
        return target_path

    @staticmethod
    def reject_symlink_components(path: Path) -> None:
        reject_path_symlinks(path, label="Target path")

    @classmethod
    def prepare_target_root(cls, target_root: Path, *, create: bool) -> Path:
        return resolve_root(target_root, create=create, label="Target root")

    @classmethod
    def validated_target_path(
        cls,
        target_file: Path,
        target_root: Path,
        *,
        create_parent: bool,
    ) -> Path:
        resolved_root = cls.prepare_target_root(target_root, create=False)
        return resolve_confined(
            resolved_root,
            target_file,
            must_exist=False,
            allow_root=False,
            create_parent=create_parent,
            label="Target path",
        )

    def transfer(
        self,
        source_file: Path,
        target_file: Path,
        mode: str,
        target_root: Path | None = None,
    ) -> str:
        boundary = target_root or target_file.parent
        resolved_root = self.prepare_target_root(boundary, create=True)
        target_file = self.validated_target_path(
            target_file,
            boundary,
            create_parent=True,
        )
        if os.path.lexists(target_file):
            try:
                if os.path.samefile(source_file, target_file):
                    return "already present"
            except OSError:
                pass
            if mode == "copy" and self.identical_regular_files(
                source_file,
                target_file,
            ):
                return "recovered existing copy"
            raise FileExistsError(f"target already exists: {target_file}")

        if mode == "hardlink":
            target_file = self.validated_target_path(
                target_file,
                resolved_root,
                create_parent=False,
            )
            os.link(source_file, target_file)
            return "hardlinked"
        if mode == "copy":
            temp_file = target_file.with_name(
                f".{target_file.name}.part-{os.getpid()}-{time.time_ns()}"
            )
            try:
                target_file = self.validated_target_path(
                    target_file,
                    resolved_root,
                    create_parent=False,
                )
                temp_file = self.validated_target_path(
                    temp_file,
                    resolved_root,
                    create_parent=False,
                )
                if os.path.lexists(temp_file):
                    raise FileExistsError(f"temporary target already exists: {temp_file}")
                shutil.copy2(source_file, temp_file)
                temp_file = self.validated_target_path(
                    temp_file,
                    resolved_root,
                    create_parent=False,
                )
                target_file = self.validated_target_path(
                    target_file,
                    resolved_root,
                    create_parent=False,
                )
                if os.path.lexists(target_file):
                    raise FileExistsError(f"target already exists: {target_file}")
                temp_file.replace(target_file)
            except Exception:
                try:
                    temp_file.unlink()
                except FileNotFoundError:
                    pass
                raise
            return "copied"
        raise ValueError(f"Unsupported mode: {mode}")

    @staticmethod
    def identical_regular_files(
        source_file: Path,
        target_file: Path,
        *,
        chunk_size: int = 1024 * 1024,
    ) -> bool:
        return stable_regular_files_equal(
            source_file,
            target_file,
            chunk_size=chunk_size,
        )

    def run_ffmpeg_split(
        self,
        ffmpeg_path: str,
        audio_source: Path,
        output_file: Path,
        start: float,
        end: float | None,
        track: CueTrack,
        sheet: CueSheet,
        total_tracks: int,
        flac_compression_level: int = 6,
    ) -> None:
        options = CueSplitOptions(
            ffmpeg_path=ffmpeg_path,
            flac_compression_level=flac_compression_level,
        )
        self.cue_processor.run_ffmpeg_split(
            options, audio_source, output_file, start, end, track, sheet, total_tracks
        )

    def split_cue_if_needed(
        self,
        source_cue: Path,
        target_cue: Path,
        config: dict[str, Any],
        processed_sources: set[str],
        verbose_actions: bool,
        target_root: Path | None = None,
    ) -> tuple[int, int, int]:
        resolved_target_root: Path | None = None
        if source_cue.suffix.lower() == ".cue":
            boundary = target_root or target_cue.parent
            resolved_target_root = self.prepare_target_root(boundary, create=True)
            target_cue = self.validated_target_path(
                target_cue,
                boundary,
                create_parent=True,
            )
        options = CueSplitOptions.from_mapping(config.get("cue_split", {}))
        marker_prefix = f"{source_cue}#track-"
        completed_tracks: set[int] = set()
        for source in processed_sources:
            if not source.startswith(marker_prefix):
                continue
            track_number = source[len(marker_prefix):]
            if track_number.isdigit():
                completed_tracks.add(int(track_number))

        result = self.cue_processor.split(
            source_cue,
            target_cue,
            options,
            completed_tracks=completed_tracks,
            sheet=self._cue_sheet_cache.get(source_cue),
        )
        for created_file in result.files:
            if resolved_target_root is not None:
                self.validated_target_path(
                    created_file.output_path,
                    resolved_target_root,
                    create_parent=False,
                )
            self.repository.record_file(
                created_file.marker,
                created_file.output_path,
                "cue_split",
                "success",
                "cue split",
            )
            processed_sources.add(str(created_file.marker))
            if verbose_actions:
                self.logger.info("cue split: %s -> %s", source_cue, created_file.output_path)
        for issue in result.issues:
            if issue.verbose_only and not verbose_actions:
                continue
            log = getattr(self.logger, issue.level)
            log(issue.message)
        return result.created, result.skipped, result.failed
    def cue_image_audio_sources(
        self,
        source_root: Path,
        config: dict[str, Any],
        scan_roots: list[Path] | None = None,
    ) -> set[str]:
        cue_config = config.get("cue_split", {})
        if (
            not cue_config.get("enabled", True)
            or not cue_config.get("skip_source_audio", True)
        ):
            return set()

        image_audio_sources: set[str] = set()
        for scan_root in scan_roots or [source_root]:
            for source_cue in self.iter_candidate_files(scan_root, config, rules_root=source_root):
                if source_cue.suffix.lower() != ".cue":
                    continue

                try:
                    sheet = parse_cue(source_cue)
                    self._cue_sheet_cache[source_cue] = sheet
                except Exception as exc:
                    self.logger.warning("Failed to parse CUE for source-audio skip: %s (%s)", source_cue, exc)
                    continue

                tracks = sheet.tracks or []
                cue_files = {
                    normalized_cue_file_name(track.file_name)
                    for track in tracks
                    if track.file_name.strip()
                }
                if len(cue_files) != 1:
                    continue

                for track in tracks:
                    audio_source = resolve_cue_audio(source_cue, track.file_name)
                    if audio_source:
                        image_audio_sources.add(str(audio_source))
                        break

        return image_audio_sources

    @staticmethod
    def normalize_remote_path(value: str) -> str:
        normalized = str(value or "").replace("\\", "/").rstrip("/")
        return normalized or "/"

    @classmethod
    def path_is_under(cls, path: str, root: str) -> bool:
        normalized_path = cls.normalize_remote_path(path)
        normalized_root = cls.normalize_remote_path(root)
        return normalized_path == normalized_root or normalized_path.startswith(
            f"{normalized_root}/"
        )

    def torrent_matches_source_roots(
        self,
        torrent: dict[str, Any],
        source_roots: list[str],
    ) -> bool:
        candidates = [
            str(torrent.get("content_path") or ""),
            str(torrent.get("save_path") or ""),
        ]
        return any(
            candidate and self.path_is_under(candidate, source_root)
            for candidate in candidates
            for source_root in source_roots
        )

    def torrent_scan_paths(self, torrents: list[dict[str, Any]]) -> list[str]:
        paths = []
        for torrent in torrents:
            for key in ("content_path", "save_path"):
                value = str(torrent.get(key) or "").strip()
                if value:
                    paths.append(value)
                    break
        return paths

    def scan_roots_for_mapping(
        self,
        scan_paths: list[str] | None,
        source_root: Path,
    ) -> list[Path]:
        if scan_paths is None:
            return [source_root]

        selected: list[Path] = []
        for value in scan_paths:
            path = Path(value)
            if not path.is_absolute():
                continue
            if path != source_root and not self.path_relative_to(path, source_root):
                continue
            if not path.exists():
                self.logger.warning("qBittorrent completed path is not visible: %s", path)
                continue
            if any(path == existing or self.path_relative_to(path, existing) for existing in selected):
                continue
            selected = [
                existing
                for existing in selected
                if not self.path_relative_to(existing, path)
            ]
            selected.append(path)

        return selected

    @staticmethod
    def torrent_is_completed(torrent: dict[str, Any], min_age_seconds: int) -> bool:
        state = str(torrent.get("state") or "")
        if state in {"error", "missingFiles"}:
            return False

        try:
            progress = float(torrent.get("progress") or 0)
        except (TypeError, ValueError):
            progress = 0
        try:
            amount_left = int(torrent.get("amount_left") or 0)
        except (TypeError, ValueError):
            amount_left = 0
        try:
            completion_on = int(torrent.get("completion_on") or 0)
        except (TypeError, ValueError):
            completion_on = 0

        if progress < 1 and amount_left > 0:
            return False
        if completion_on > 0 and min_age_seconds > 0:
            return time.time() - completion_on >= min_age_seconds
        return progress >= 1 or amount_left == 0

    @staticmethod
    def torrent_matches_qb_filters(torrent: dict[str, Any], category: str, tag: str) -> bool:
        if category and str(torrent.get("category") or "") != category:
            return False
        if tag:
            tags = {
                item.strip()
                for item in str(torrent.get("tags") or "").split(",")
                if item.strip()
            }
            if tag not in tags:
                return False
        return True

    def qb_client_from_config(self, qb_config: dict[str, Any]) -> QBittorrentClient:
        base_url = str(qb_config.get("base_url") or "").strip()
        if not base_url:
            raise ValueError("qBittorrent base_url is required when active integration is enabled")
        return QBittorrentClient(
            base_url=base_url,
            username=str(qb_config.get("username") or ""),
            password=str(qb_config.get("password") or ""),
            api_key=str(qb_config.get("api_key") or ""),
            timeout=int(qb_config.get("timeout", 10) or 10),
            max_attempts=int(qb_config.get("network_max_attempts", 3) or 3),
            retry_base_seconds=float(
                qb_config.get("network_retry_seconds", 1) or 0
            ),
            retry_max_seconds=float(
                qb_config.get("network_retry_max_seconds", 5) or 0
            ),
        )

    def record_qb_connection_status(self, status: str, error: str = "") -> None:
        now = datetime.now().isoformat(timespec="seconds")
        self.set_app_state_value("qb_last_attempt_at", now)
        self.set_app_state_value("qb_last_status", status)
        self.set_app_state_value("qb_last_error", error[:500])
        if status == "ok":
            self.set_app_state_value("qb_last_success_at", now)

    def pending_qb_torrents(
        self, config: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], int | None, bool]:
        qb_config = config.get("qbittorrent", {})
        client = self.qb_client_from_config(qb_config)
        seen_hashes = self.seen_qb_hashes()
        delayed_hashes = self.repository.delayed_qb_hashes()
        source_roots = list((config.get("paths_mapping") or {}).keys())
        min_age_seconds = int(qb_config.get("min_completion_age_seconds", 60) or 0)
        category = str(qb_config.get("category") or "").strip()
        tag = str(qb_config.get("tag") or "").strip()
        poll_mode = str(qb_config.get("poll_mode") or "sync")
        next_rid: int | None = None
        has_deferred = False

        if poll_mode == "sync":
            rid = int(self.app_state_value("qb_sync_rid", "0") or 0)
            payload = client.sync_maindata(rid)
            next_rid = int(payload.get("rid") or rid)
            torrents_payload = payload.get("torrents") or {}
            if payload.get("full_update"):
                candidates = []
                for torrent_hash, torrent in torrents_payload.items():
                    if isinstance(torrent, dict):
                        item = dict(torrent)
                        item.setdefault("hash", torrent_hash)
                        candidates.append(item)
            else:
                changed_hashes = [str(value) for value in torrents_payload.keys()]
                candidates = client.torrents_info(category=category, tag=tag, hashes=changed_hashes) if changed_hashes else []
        else:
            candidates = client.torrents_info(category=category, tag=tag)

        pending = []
        for torrent in candidates:
            torrent_hash = str(torrent.get("hash") or "").lower()
            if not torrent_hash or torrent_hash in seen_hashes:
                continue
            if torrent_hash in delayed_hashes:
                has_deferred = True
                continue
            if not self.torrent_matches_qb_filters(torrent, category, tag):
                continue
            if source_roots and not self.torrent_matches_source_roots(torrent, source_roots):
                continue
            if not self.torrent_is_completed(torrent, min_age_seconds):
                has_deferred = True
                continue
            pending.append(torrent)

        return pending, next_rid, has_deferred

    def scan_completed_qb_torrents(self) -> RunResult:
        config = self.load_config()
        qb_config = config.get("qbittorrent", {})
        if not qb_config.get("enabled", False):
            return self.scan_and_organize()

        try:
            pending, next_rid, has_deferred = self.pending_qb_torrents(config)
        except (urllib.error.URLError, TimeoutError, RuntimeError, ValueError) as exc:
            message = f"qBittorrent poll failed: {exc}"
            self.record_qb_connection_status("failed", str(exc))
            self.logger.warning(message)
            return RunResult(failed=1, message=message)

        self.record_qb_connection_status("ok")

        if not pending:
            if next_rid is not None and not has_deferred:
                self.set_app_state_value("qb_sync_rid", str(next_rid))
            self.logger.info("qBittorrent poll found no new completed music torrents")
            return RunResult(message="no new completed qb torrents")

        names = ", ".join(str(item.get("name") or item.get("hash")) for item in pending[:5])
        if len(pending) > 5:
            names = f"{names}, ..."
        self.logger.info("qBittorrent found %s completed torrent(s): %s", len(pending), names)

        scan_mode = str(qb_config.get("scan_mode") or "torrent_paths")
        scan_paths = None
        if scan_mode != "full":
            scan_paths = self.torrent_scan_paths(pending)
            self.logger.info("qBittorrent scoped scan paths: %s", scan_paths)

        result = self.scan_and_organize(scan_paths=scan_paths)
        result.details["torrent_names"] = [
            str(item.get("name") or item.get("hash") or "")
            for item in pending
            if item.get("name") or item.get("hash")
        ]
        result.details["torrent_count"] = len(pending)
        if result.failed == 0:
            self.record_qb_torrents(pending, "seen", result.message)
            if next_rid is not None and not has_deferred:
                self.set_app_state_value("qb_sync_rid", str(next_rid))
        else:
            retry_states = self.repository.record_qb_failures(
                pending,
                result.message,
                max_attempts=int(qb_config.get("retry_max_attempts", 5) or 5),
                base_delay_seconds=int(qb_config.get("retry_base_seconds", 60) or 60),
                max_delay_seconds=int(qb_config.get("retry_max_seconds", 3600) or 3600),
            )
            result.details["torrent_retry_states"] = retry_states
        return result

    def beets_config_file(self, review_config: dict[str, Any]) -> Path:
        configured = str(review_config.get("config_path") or "").strip()
        if configured:
            return Path(configured)
        return self.database_path.parent / "review-beets-config.yaml"

    def write_beets_config(self, review_config: dict[str, Any]) -> Path:
        config_path = self.beets_config_file(review_config)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        library = str(review_config.get("library") or "").strip()
        if not library:
            library = str(self.database_path.parent / "review-beets-library.db")
        import_mode = str(review_config.get("import_mode") or "hardlink").lower()
        if import_mode not in {"copy", "hardlink", "move"}:
            import_mode = "hardlink"
        write_tags = bool(review_config.get("write_tags", False))
        payload = {
            "directory": str(review_config.get("directory") or "/media/library/music"),
            "library": library,
            "plugins": ["inline", "musicbrainz", "picardpreset"],
            "pluginpath": [
                str(Path(__file__).parent / "music_organizer" / "beetsplug")
            ],
            "asciify_paths": False,
            "import": {
                "copy": import_mode == "copy",
                "move": import_mode == "move",
                "hardlink": import_mode == "hardlink",
                "link": False,
                "write": write_tags,
                "quiet": True,
                "timid": False,
            },
            "paths": {
                "default": str(review_config.get("path_format") or DEFAULT_BEETS_PATH_FORMAT),
            },
            "item_fields": {
                "album_dir": "albumartist or artist",
                "disc_prefix": "f'{disc}-' if disctotal and int(disctotal) > 1 else ''",
                "track_prefix": "f'{track:02d} ' if albumartist and track else ''",
            },
        }
        with config_path.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(payload, fh, allow_unicode=True, sort_keys=False)
        return config_path

    def scan_and_organize(self, scan_paths: list[str] | None = None) -> RunResult:
        run_id = self.create_run()
        result = RunResult(message="ok")
        album_names: set[str] = set()
        pending_records: list[tuple[Path, Path, str, str, str]] = []

        def flush_pending_records() -> None:
            if not pending_records:
                return
            self.record_files(pending_records)
            pending_records.clear()

        try:
            config = self.load_config()
            mode = str(config.get("mode", "hardlink")).lower()
            if mode not in {"hardlink", "copy"}:
                raise ValueError("mode must be hardlink or copy")

            mappings = config.get("paths_mapping") or {}
            self._cue_sheet_cache.clear()
            processed_sources = self.processed_sources()
            verbose_actions = bool(config.get("logging", {}).get("verbose_file_actions", False))
            progress_interval = int(config.get("logging", {}).get("progress_interval", 500) or 500)
            found_scan_root = False
            if scan_paths is None:
                self.logger.info("Organizer run started; mode=%s; scope=all", mode)
            else:
                self.logger.info(
                    "Organizer run started; mode=%s; scope=%s path(s)",
                    mode,
                    len(scan_paths),
                )
            for source, target in mappings.items():
                if self.stop_requested():
                    raise InterruptedError("stopped by user")

                source_root = Path(source)
                target_root = Path(target)
                if not source_root.is_absolute() or not target_root.is_absolute():
                    self.logger.error("Paths must be absolute: %s -> %s", source, target)
                    result.failed += 1
                    continue
                if self.path_is_same_or_under(target_root, source_root):
                    self.logger.error(
                        "Refusing source/target overlap: %s -> %s",
                        source_root,
                        target_root,
                    )
                    result.failed += 1
                    continue
                if not source_root.exists():
                    self.logger.warning("Source path does not exist: %s", source_root)
                    continue

                try:
                    target_root = self.prepare_target_root(target_root, create=True)
                except (OSError, ValueError, RuntimeError) as exc:
                    self.logger.error("Refusing unsafe target root %s: %s", target, exc)
                    result.failed += 1
                    continue
                scan_roots = self.scan_roots_for_mapping(scan_paths, source_root)
                if not scan_roots:
                    continue
                found_scan_root = True

                cue_image_audio_sources = self.cue_image_audio_sources(
                    source_root,
                    config,
                    scan_roots=scan_roots,
                )
                for scan_root in scan_roots:
                    for source_file in self.iter_candidate_files(
                        scan_root,
                        config,
                        rules_root=source_root,
                    ):
                        if self.stop_requested():
                            raise InterruptedError("stopped by user")

                        result.scanned += 1
                        target_file = self.target_for(source_file, source_root, target_root, config)

                        if str(source_file) in cue_image_audio_sources:
                            result.skipped += 1
                            if verbose_actions:
                                self.logger.info(
                                    "Skipped CUE source audio because splitting is enabled: %s",
                                    source_file,
                                )
                            continue

                        if str(source_file) in processed_sources:
                            created, skipped, failed = self.split_cue_if_needed(
                                source_file,
                                target_file,
                                config,
                                processed_sources,
                                verbose_actions,
                                target_root,
                            )
                            result.organized += created
                            result.skipped += skipped + 1
                            result.failed += failed
                            if created and source_file.suffix.lower() == ".cue":
                                album_names.add(target_file.parent.name)
                            if result.scanned % progress_interval == 0:
                                self.update_run_progress(run_id, result)
                                self.logger.info(
                                    "Progress: scanned=%s organized=%s skipped=%s failed=%s",
                                    result.scanned,
                                    result.organized,
                                    result.skipped,
                                    result.failed,
                                )
                            continue

                        try:
                            message = self.transfer(
                                source_file,
                                target_file,
                                mode,
                                target_root,
                            )
                            pending_records.append(
                                (source_file, target_file, mode, "success", message)
                            )
                            if len(pending_records) >= 500:
                                flush_pending_records()
                            processed_sources.add(str(source_file))
                            result.organized += 1
                            album_names.add(target_file.parent.name)
                            created, skipped, failed = self.split_cue_if_needed(
                                source_file,
                                target_file,
                                config,
                                processed_sources,
                                verbose_actions,
                                target_root,
                            )
                            result.organized += created
                            result.skipped += skipped
                            result.failed += failed
                            if verbose_actions:
                                self.logger.info("%s: %s -> %s", message, source_file, target_file)
                        except InterruptedError:
                            raise
                        except OSError as exc:
                            result.failed += 1
                            self.logger.exception("Failed to organize %s: %s", source_file, exc)
                        except Exception as exc:
                            result.failed += 1
                            self.logger.exception("Unexpected error for %s: %s", source_file, exc)

                        if result.scanned % progress_interval == 0:
                            self.update_run_progress(run_id, result)
                            self.logger.info(
                                "Progress: scanned=%s organized=%s skipped=%s failed=%s",
                                result.scanned,
                                result.organized,
                                result.skipped,
                                result.failed,
                            )
            if scan_paths is not None and not found_scan_root:
                result.failed += 1
                result.message = "no scoped qBittorrent paths were visible"
            elif result.failed:
                result.message = f"completed with {result.failed} failures"
            self.logger.info(
                "Organizer run finished; scanned=%s organized=%s skipped=%s failed=%s",
                result.scanned,
                result.organized,
                result.skipped,
                result.failed,
            )
            result.details["album_names"] = sorted(album_names)
            return result
        except InterruptedError:
            result.message = "stopped by user"
            self.logger.info(
                "Organizer run stopped; scanned=%s organized=%s skipped=%s failed=%s",
                result.scanned,
                result.organized,
                result.skipped,
                result.failed,
            )
            result.details["album_names"] = sorted(album_names)
            return result
        except Exception as exc:
            result.failed += 1
            result.message = str(exc)
            self.logger.exception("Organizer run failed: %s", exc)
            result.details["album_names"] = sorted(album_names)
            return result
        finally:
            try:
                flush_pending_records()
            except Exception as exc:
                result.failed += len(pending_records) or 1
                result.message = f"database record batch failed: {exc}"
                self.logger.exception(
                    "Failed to persist organized file batch: %s", exc
                )
            self.finish_run(run_id, result)

    def stats(
        self,
        *,
        config: dict[str, Any] | None = None,
        snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        config = config or self.load_config()
        snapshot = snapshot or self.repository.dashboard_snapshot()
        public_snapshot = {
            key: value
            for key, value in snapshot.items()
            if key not in {"app_state", "job_status", "review_counts"}
        }
        qbittorrent = dict(config.get("qbittorrent", {}))
        qbittorrent.pop("password", None)
        qbittorrent.pop("api_key", None)
        return {
            "paths_mapping": config.get("paths_mapping", {}),
            "mode": config.get("mode", "hardlink"),
            "qbittorrent": qbittorrent,
            **public_snapshot,
        }

    def history(self, page: int = 1, per_page: int = 50, query: str = "") -> dict[str, Any]:
        return self.repository.history(page, per_page, query)
    def recent_logs(self, limit: int = 200) -> list[str]:
        return tail_lines(self.log_path, limit)
