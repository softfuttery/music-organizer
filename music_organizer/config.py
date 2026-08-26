"""YAML configuration loading, defaults and persistence."""

import copy
import os
import stat
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

import yaml

from .auth import write_secret_atomic, write_secret_bytes_atomic
from .naming import LEGACY_PATH_FORMAT, PICARD_PRESET3_PATH_FORMAT

DEFAULT_INCLUDE_EXTS = [
    ".flac", ".wav", ".ape", ".m4a", ".aac", ".ogg", ".opus", ".dsf", ".dff",
    ".wv", ".tta", ".alac", ".cue", ".jpg", ".jpeg", ".png", ".webp", ".bmp",
]
DEFAULT_BEETS_PATH_FORMAT = PICARD_PRESET3_PATH_FORMAT
DEFAULT_REVIEW_EXTRA_FILE_PATTERNS = ["*.jpg", "*.png"]
_CONFIG_SAVE_LOCK = threading.RLock()


@contextmanager
def _config_file_lock(config_path: Path):
    """Serialize config/secret snapshots across all service processes."""
    lock_path = config_path.with_name(f".{config_path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _runtime_secret_path(environment_name: str, filename: str) -> str:
    configured = str(os.environ.get(environment_name) or "").strip()
    if configured:
        return configured
    database_path = Path(
        os.environ.get("DATABASE_PATH", "/app/data/organizer.sqlite3")
    )
    return str(database_path.parent / "secrets" / filename)


def _secret_specs(config: dict[str, Any]):
    qb = config["qbittorrent"]
    review = config["review"]
    translation = config["translation"]
    yield qb, "password", "password_file"
    yield qb, "api_key", "api_key_file"
    yield review, "proxy_password", "proxy_password_file"
    yield translation, "api_key", "api_key_file"


def _read_secret(path_value: object) -> str:
    value = str(path_value or "").strip()
    if not value:
        return ""
    try:
        return Path(value).expanduser().read_text(encoding="utf-8").rstrip("\r\n")
    except OSError:
        return ""


def _normalize_url_userinfo(
    section: dict[str, Any],
    *,
    url_key: str,
    username_key: str,
    password_key: str,
    label: str,
) -> None:
    """Move legacy URL credentials into transient fields before YAML migration."""
    value = str(section.get(url_key) or "").strip()
    if not value:
        return
    try:
        parsed = urlsplit(value)
        username = parsed.username
        password = parsed.password
    except ValueError as exc:
        raise ValueError(f"{label}格式无效，无法安全迁移账号密码") from exc
    if username is None and password is None:
        return
    if not str(section.get(username_key) or "") and username is not None:
        section[username_key] = unquote(username)
    if not str(section.get(password_key) or "") and password is not None:
        section[password_key] = unquote(password)
    # Strip userinfo without reading ``parsed.port``. This also migrates a legacy
    # credential-bearing URL whose port text is malformed, so the password can
    # never be written back to YAML while normal URL validation still rejects it.
    host = parsed.netloc.rsplit("@", 1)[-1]
    if not host:
        raise ValueError(f"{label}缺少主机名")
    section[url_key] = urlunsplit(
        (parsed.scheme, host, parsed.path, parsed.query, parsed.fragment)
    )


def _normalize_proxy_userinfo(review: dict[str, Any]) -> None:
    _normalize_url_userinfo(
        review,
        url_key="proxy_url",
        username_key="proxy_username",
        password_key="proxy_password",
        label="代理地址",
    )


def _normalize_qbittorrent_userinfo(qbittorrent: dict[str, Any]) -> None:
    _normalize_url_userinfo(
        qbittorrent,
        url_key="base_url",
        username_key="username",
        password_key="password",
        label="qBittorrent 地址",
    )


def _hydrate_credentials(config: dict[str, Any]) -> None:
    for section, value_key, file_key in _secret_specs(config):
        if not str(section.get(value_key) or ""):
            section[value_key] = _read_secret(section.get(file_key))


def _contains_plaintext_credentials(config: dict[str, Any]) -> bool:
    qbittorrent_value = config.get("qbittorrent", {})
    review_value = config.get("review", {})
    qbittorrent = (
        qbittorrent_value if isinstance(qbittorrent_value, dict) else {}
    )
    review = review_value if isinstance(review_value, dict) else {}
    translation_value = config.get("translation", {})
    translation = (
        translation_value if isinstance(translation_value, dict) else {}
    )
    if any(
        str(qbittorrent.get(key) or "")
        for key in ("password", "api_key")
    ) or str(review.get("proxy_password") or "") or str(
        translation.get("api_key") or ""
    ):
        return True
    for value in (
        qbittorrent.get("base_url"),
        review.get("proxy_url"),
    ):
        text = str(value or "").strip()
        if not text:
            continue
        try:
            parsed = urlsplit(text)
            if parsed.username is not None or parsed.password is not None:
                return True
        except ValueError:
            # A malformed credential-bearing URL must fail closed during the
            # migration attempt instead of remaining silently in YAML.
            if "@" in text:
                return True
    return False


def _externalize_credentials(
    config: dict[str, Any],
) -> tuple[dict[str, Any], list[tuple[Path, str]]]:
    persisted = copy.deepcopy(config)
    secret_writes: list[tuple[Path, str]] = []
    _normalize_qbittorrent_userinfo(persisted["qbittorrent"])
    _normalize_proxy_userinfo(persisted["review"])
    for section, value_key, file_key in _secret_specs(persisted):
        value = str(section.pop(value_key, "") or "")
        path_value = str(section.get(file_key) or "").strip()
        if value:
            if not path_value:
                raise ValueError(f"Secret file is not configured for {value_key}")
            secret_writes.append((Path(path_value).expanduser(), value))
    return persisted, secret_writes


def _secret_snapshots(
    config_path: Path,
    secret_writes: list[tuple[Path, str]],
) -> dict[Path, bytes | None]:
    snapshots: dict[Path, bytes | None] = {}
    normalized_paths: set[str] = set()
    normalized_config = os.path.normcase(str(config_path.absolute()))
    for secret_path, _value in secret_writes:
        normalized = os.path.normcase(str(secret_path.absolute()))
        if normalized == normalized_config:
            raise ValueError("密钥文件不能与配置文件使用同一路径")
        if normalized in normalized_paths:
            raise ValueError("不同密钥不能共用同一个密钥文件")
        normalized_paths.add(normalized)
        try:
            metadata = secret_path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError(f"密钥文件不能是符号链接: {secret_path}")
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError(f"密钥文件路径不是普通文件: {secret_path}")
            snapshots[secret_path] = secret_path.read_bytes()
        except FileNotFoundError:
            snapshots[secret_path] = None
    return snapshots


def _restore_secret_snapshots(
    snapshots: dict[Path, bytes | None],
    written_paths: list[Path],
) -> list[OSError]:
    errors: list[OSError] = []
    for secret_path in reversed(written_paths):
        try:
            original = snapshots[secret_path]
            if original is None:
                secret_path.unlink(missing_ok=True)
            else:
                write_secret_bytes_atomic(secret_path, original)
        except OSError as exc:
            errors.append(exc)
    return errors


def apply_defaults(config: dict[str, Any]) -> dict[str, Any]:
    # Deprecated since the 2026-07 review-only migration. Keep this compatibility
    # shim for two release cycles, then remove the legacy top-level "beets" key.
    legacy_beets_value = config.pop("beets", {})
    legacy_beets = (
        legacy_beets_value if isinstance(legacy_beets_value, dict) else {}
    )
    config.setdefault("paths_mapping", {})
    config.setdefault("mode", "hardlink")
    config.setdefault("keep_dir_struct", True)
    config.setdefault("mkdir_if_single", True)
    config.setdefault("exclude", {})
    config["exclude"].setdefault("globs", [])
    config["exclude"].setdefault("exts", [])
    config.setdefault("include", {})
    config["include"].setdefault("globs", [])
    config["include"].setdefault("exts", DEFAULT_INCLUDE_EXTS.copy())
    config.setdefault("cue_split", {})
    cue = config["cue_split"]
    cue.setdefault("enabled", True)
    cue.setdefault("output_subdir", "")
    cue.setdefault("filename_template", "{track:02d} - {title}")
    cue.setdefault("skip_existing", True)
    cue.setdefault("ffmpeg_path", "ffmpeg")
    cue.setdefault("flac_compression_level", 6)
    cue.setdefault("split_multifile_cues", False)
    cue.setdefault("skip_source_audio", True)
    config.setdefault("qbittorrent", {})
    qb = config["qbittorrent"]
    qb.setdefault("enabled", False)
    qb.setdefault("base_url", "")
    qb.setdefault("username", "")
    qb.setdefault("password", "")
    _normalize_qbittorrent_userinfo(qb)
    qb.setdefault(
        "password_file",
        _runtime_secret_path(
            "QBITTORRENT_PASSWORD_FILE", "qbittorrent_password"
        ),
    )
    qb.setdefault("api_key", "")
    qb.setdefault(
        "api_key_file",
        _runtime_secret_path("QBITTORRENT_API_KEY_FILE", "qbittorrent_api_key"),
    )
    qb.setdefault("timeout", 10)
    qb.setdefault("min_completion_age_seconds", 60)
    qb.setdefault("scan_mode", "torrent_paths")
    qb.setdefault("poll_mode", "sync")
    qb.setdefault("category", "")
    qb.setdefault("tag", "")
    qb.setdefault("retry_max_attempts", 5)
    qb.setdefault("retry_base_seconds", 60)
    qb.setdefault("retry_max_seconds", 3600)
    config.setdefault("review", {})
    review = config["review"]
    review.setdefault("enabled", False)
    review.setdefault("source_roots", [])
    review.setdefault("identify_workers", 3)
    review.setdefault("poll_seconds", 1)
    review.setdefault("max_attempts", 3)
    review.setdefault("import_timeout_seconds", 3600)
    review.setdefault("auto_discover", True)
    review.setdefault("discovery_interval_seconds", 15)
    review.setdefault("discovery_stable_seconds", 60)
    review.setdefault("proxy_url", "")
    review.setdefault("proxy_username", "")
    review.setdefault("proxy_password", "")
    review.setdefault(
        "proxy_password_file",
        _runtime_secret_path(
            "REVIEW_PROXY_PASSWORD_FILE", "review_proxy_password"
        ),
    )
    _normalize_proxy_userinfo(review)
    # Older releases kept these beets import settings in a separate section.
    # Resolve them into the review workflow before the first post-upgrade save.
    review["directory"] = str(
        review.get("directory")
        or legacy_beets.get("directory")
        or "/media/library/music"
    )
    review["library"] = str(
        review.get("library") or legacy_beets.get("library") or ""
    )
    review["config_path"] = str(
        review.get("config_path") or legacy_beets.get("config_path") or ""
    )
    review["path_format"] = str(
        review.get("path_format")
        or legacy_beets.get("path_format")
        or DEFAULT_BEETS_PATH_FORMAT
    )
    if review["path_format"] == LEGACY_PATH_FORMAT:
        review["path_format"] = DEFAULT_BEETS_PATH_FORMAT
    review["import_mode"] = str(
        review.get("import_mode") or legacy_beets.get("import_mode") or "hardlink"
    ).lower()
    if review["import_mode"] not in {"copy", "hardlink", "move"}:
        review["import_mode"] = "hardlink"
    if "write_tags" not in review:
        review["write_tags"] = bool(legacy_beets.get("write_tags", False))
    patterns = review.get("extra_file_patterns", DEFAULT_REVIEW_EXTRA_FILE_PATTERNS)
    if isinstance(patterns, str):
        patterns = patterns.replace(",", " ").replace(";", " ").split()
    elif not isinstance(patterns, list):
        patterns = DEFAULT_REVIEW_EXTRA_FILE_PATTERNS.copy()
    review["extra_file_patterns"] = [
        str(pattern).strip() for pattern in patterns if str(pattern).strip()
    ]
    review.setdefault("move_extra_files", False)
    review.setdefault("cleanup_source_after_import", False)
    config.setdefault("translation", {})
    translation = config["translation"]
    translation.setdefault("enabled", False)
    translation.setdefault("base_url", "https://api.deepseek.com")
    translation.setdefault("model", "deepseek-v4-flash")
    translation.setdefault("api_key", "")
    translation.setdefault(
        "api_key_file",
        _runtime_secret_path(
            "LYRICS_TRANSLATION_API_KEY_FILE", "lyrics_translation_api_key"
        ),
    )
    translation.setdefault("target_language", "简体中文")
    translation.setdefault("style", "natural")
    translation.setdefault("timeout", 120)
    config.setdefault("logging", {})
    config["logging"].setdefault("verbose_file_actions", False)
    config["logging"].setdefault("progress_interval", 500)
    config.setdefault("notifications", {})
    config["notifications"].setdefault("magicpush", {})
    magicpush = config["notifications"]["magicpush"]
    magicpush.setdefault("enabled", False)
    magicpush.setdefault("base_url", "")
    magicpush.setdefault(
        "token_file",
        os.environ.get("MAGICPUSH_TOKEN_FILE", "/run/secrets/magicpush/token"),
    )
    magicpush.setdefault("title", "Music Organizer")
    magicpush.setdefault("type", "text")
    magicpush.setdefault("timeout", 10)
    magicpush.setdefault("notify_no_changes", False)
    config.setdefault("schedule", {"enabled": True, "cron": "*/30 * * * *"})
    return config


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with _CONFIG_SAVE_LOCK, _config_file_lock(path):
        with path.open("r", encoding="utf-8") as fh:
            config = yaml.safe_load(fh) or {}
        if not isinstance(config, dict):
            raise ValueError("Config root must be a mapping")
        config = apply_defaults(config)
        _hydrate_credentials(config)
        return config


def _save_config_locked(path: Path, config: dict[str, Any]) -> None:
    persisted, secret_writes = _externalize_credentials(
        apply_defaults(copy.deepcopy(config))
    )
    snapshots = _secret_snapshots(path, secret_writes)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    written_paths: list[Path] = []
    try:
        for secret_path, value in secret_writes:
            written_paths.append(secret_path)
            write_secret_atomic(secret_path, value)
        with temp_path.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(persisted, fh, allow_unicode=True, sort_keys=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_path, path)
    except Exception as exc:
        rollback_errors = _restore_secret_snapshots(snapshots, written_paths)
        if rollback_errors:
            raise RuntimeError(
                "配置保存失败，且一个或多个密钥文件无法回滚"
            ) from exc
        raise
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def save_config(path: Path, config: dict[str, Any]) -> None:
    with _CONFIG_SAVE_LOCK, _config_file_lock(path):
        _save_config_locked(path, config)


def migrate_plaintext_credentials(path: Path) -> bool:
    """Move legacy YAML credentials into restricted files once at startup."""
    if not path.exists():
        return False
    with _CONFIG_SAVE_LOCK, _config_file_lock(path):
        with path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
        if not isinstance(raw, dict):
            raise ValueError("Config root must be a mapping")
        if not _contains_plaintext_credentials(raw):
            return False
        config = apply_defaults(raw)
        _hydrate_credentials(config)
        _save_config_locked(path, config)
        return True


def normalize_exts(exts: list[Any]) -> set[str]:
    normalized = set()
    for item in exts:
        ext = str(item).strip().lower()
        if not ext:
            continue
        if not ext.startswith("."):
            ext = f".{ext}"
        normalized.add(ext)
    return normalized
