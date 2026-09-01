"""YAML configuration loading, defaults and persistence."""

import copy
import hashlib
import os
import re
import stat
import threading
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

import yaml

from .auth import write_secret_atomic, write_secret_bytes_atomic
from .locking import exclusive_file_lock
from .naming import LEGACY_PATH_FORMAT, PICARD_PRESET3_PATH_FORMAT

DEFAULT_INCLUDE_EXTS = [
    ".flac", ".wav", ".ape", ".m4a", ".aac", ".ogg", ".opus", ".dsf", ".dff",
    ".wv", ".tta", ".alac", ".cue", ".jpg", ".jpeg", ".png", ".webp", ".bmp",
]
DEFAULT_BEETS_PATH_FORMAT = PICARD_PRESET3_PATH_FORMAT
DEFAULT_REVIEW_EXTRA_FILE_PATTERNS = ["*.jpg", "*.png"]
REVIEW_DISCOVERY_MODES = {"direct", "artist_album"}
_CONFIG_SAVE_LOCK = threading.RLock()
_CONFIG_CACHE: dict[
    Path,
    tuple[
        tuple[int, int, int, int, int],
        tuple[tuple[str, str], ...],
        tuple[tuple[str, tuple[int, int, int, int, int] | None], ...],
        dict[str, Any],
    ],
] = {}
_CONFIG_ENVIRONMENT_NAMES = (
    "DATABASE_PATH",
    "MAGICPUSH_TOKEN_FILE",
    "QBITTORRENT_API_KEY_FILE",
    "QBITTORRENT_PASSWORD_FILE",
    "REVIEW_PROXY_PASSWORD_FILE",
    "TRANSLATION_API_KEY_FILE",
)


def normalize_review_source_profiles(review: dict[str, Any]) -> list[dict[str, Any]]:
    """Return normalized, backward-compatible per-directory review workflows."""
    raw_profiles = review.get("source_profiles")
    if not isinstance(raw_profiles, list):
        raw_profiles = []
    legacy_auto = bool(review.get("auto_discover", True))
    legacy_import_mode = str(review.get("import_mode") or "hardlink").lower()
    if legacy_import_mode not in {"copy", "hardlink", "move"}:
        legacy_import_mode = "hardlink"
    if not raw_profiles:
        raw_profiles = [
            {
                "path": value,
                "name": Path(str(value)).name or str(value),
                "discovery_mode": "direct",
                "auto_discover": legacy_auto,
                "import_mode": legacy_import_mode,
                "move_extra_files": bool(review.get("move_extra_files", False)),
                "cleanup_source_after_import": bool(
                    review.get("cleanup_source_after_import", False)
                ),
            }
            for value in review.get("source_roots", [])
            if str(value).strip()
        ]

    normalized: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    seen_ids: set[str] = set()
    for index, value in enumerate(raw_profiles):
        if not isinstance(value, dict):
            continue
        path = str(value.get("path") or "").strip()
        if not path or path in seen_paths:
            continue
        seen_paths.add(path)
        discovery_mode = str(value.get("discovery_mode") or "direct").lower()
        if discovery_mode not in REVIEW_DISCOVERY_MODES:
            discovery_mode = "direct"
        import_mode = str(value.get("import_mode") or legacy_import_mode).lower()
        if import_mode not in {"copy", "hardlink", "move"}:
            import_mode = legacy_import_mode
        profile_id = re.sub(
            r"[^A-Za-z0-9_-]+", "-", str(value.get("id") or "").strip()
        ).strip("-_")[:64]
        if not profile_id or profile_id in seen_ids:
            profile_id = hashlib.sha256(path.encode("utf-8")).hexdigest()[:12]
        seen_ids.add(profile_id)
        normalized.append(
            {
                "id": profile_id[:64],
                "name": str(value.get("name") or Path(path).name or f"目录 {index + 1}")[
                    :100
                ],
                "path": path,
                "discovery_mode": discovery_mode,
                "auto_discover": bool(value.get("auto_discover", legacy_auto)),
                "import_mode": import_mode,
                "move_extra_files": bool(
                    value.get(
                        "move_extra_files", review.get("move_extra_files", False)
                    )
                ),
                "cleanup_source_after_import": bool(
                    value.get(
                        "cleanup_source_after_import",
                        review.get("cleanup_source_after_import", False),
                    )
                ),
            }
        )
    return normalized


def _config_file_lock(config_path: Path):
    """Serialize config/secret snapshots across all service processes."""
    return exclusive_file_lock(
        config_path.with_name(f".{config_path.name}.lock"),
        timeout=30,
    )


def _file_revision(path: Path) -> tuple[int, int, int, int, int] | None:
    try:
        metadata = path.stat()
    except FileNotFoundError:
        return None
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
        int(metadata.st_ctime_ns),
    )


def _environment_revision() -> tuple[tuple[str, str], ...]:
    return tuple(
        (name, str(os.environ.get(name) or ""))
        for name in _CONFIG_ENVIRONMENT_NAMES
    )


def _secret_revisions(
    config: dict[str, Any],
) -> tuple[tuple[str, tuple[int, int, int, int, int] | None], ...]:
    paths = {
        str(Path(str(section.get(file_key))).expanduser().absolute())
        for section, _value_key, file_key in _secret_specs(config)
        if str(section.get(file_key) or "").strip()
    }
    return tuple((value, _file_revision(Path(value))) for value in sorted(paths))


def _invalidate_config_cache(path: Path) -> None:
    _CONFIG_CACHE.pop(path.absolute(), None)


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
    qb.setdefault("network_max_attempts", 3)
    qb.setdefault("network_retry_seconds", 1)
    qb.setdefault("network_retry_max_seconds", 5)
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
    review.setdefault("discovery_interval_seconds", 60)
    review.setdefault("discovery_full_rescan_seconds", 600)
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
    review["source_profiles"] = normalize_review_source_profiles(review)
    review["source_roots"] = [
        profile["path"] for profile in review["source_profiles"]
    ]
    review["auto_discover"] = any(
        profile["auto_discover"] for profile in review["source_profiles"]
    )
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
    key = path.absolute()
    revision = _file_revision(path)
    if revision is None:
        raise FileNotFoundError(f"Config file not found: {path}")
    environment_revision = _environment_revision()
    with _CONFIG_SAVE_LOCK:
        cached = _CONFIG_CACHE.get(key)
        if (
            cached is not None
            and cached[0] == revision
            and cached[1] == environment_revision
            and cached[2] == _secret_revisions(cached[3])
        ):
            return copy.deepcopy(cached[3])

    with _CONFIG_SAVE_LOCK, _config_file_lock(path):
        with path.open("r", encoding="utf-8") as fh:
            config = yaml.safe_load(fh) or {}
        if not isinstance(config, dict):
            raise ValueError("Config root must be a mapping")
        config = apply_defaults(config)
        _hydrate_credentials(config)
        revision = _file_revision(path)
        if revision is None:
            raise FileNotFoundError(f"Config file not found: {path}")
        _CONFIG_CACHE[key] = (
            revision,
            environment_revision,
            _secret_revisions(config),
            copy.deepcopy(config),
        )
        return copy.deepcopy(config)


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
        _invalidate_config_cache(path)


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
        _invalidate_config_cache(path)
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
