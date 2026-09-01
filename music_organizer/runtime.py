"""Runtime bind-mount readiness checks shared by all service roles."""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any


def _probe_writable_directory(path: Path) -> str:
    """Create, fsync, rename and remove a tiny file without touching user data."""
    if not path.is_dir():
        return "directory is missing"
    token = f"{os.getpid()}-{uuid.uuid4().hex}"
    created = path / f".music-organizer-ready-{token}.tmp"
    renamed = path / f".music-organizer-ready-{token}.ok"
    descriptor = -1
    try:
        descriptor = os.open(created, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(descriptor, b"ready\n")
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(created, renamed)
        renamed.unlink()
        return "ok"
    except OSError as exc:
        detail = exc.strerror or exc.__class__.__name__
        return f"not writable: {detail}"
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        for candidate in (created, renamed):
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                # The readiness result already carries the original failure.
                # Cleanup must not turn a controlled 503 into an unhandled 500.
                pass


def runtime_readiness(config_path: str | Path, database_path: str | Path) -> dict[str, Any]:
    """Report whether the mutable config and database mounts support atomic writes."""
    config = Path(config_path)
    database = Path(database_path)
    checks = {
        "config_directory": _probe_writable_directory(config.parent),
        "data_directory": _probe_writable_directory(database.parent),
    }
    lock_path = config.with_name(f".{config.name}.lock")
    if lock_path.exists() and not os.access(lock_path, os.W_OK):
        checks["config_lock"] = "not writable"
    else:
        checks["config_lock"] = "ok"
    if database.exists() and not os.access(database, os.W_OK):
        checks["database_file"] = "not writable"
    else:
        checks["database_file"] = "ok"
    failed = {name: result for name, result in checks.items() if result != "ok"}
    return {
        "status": "ok" if not failed else "error",
        "checks": checks,
        "failed": failed,
    }


def runtime_is_ready(config_path: str | Path, database_path: str | Path) -> bool:
    return runtime_readiness(config_path, database_path)["status"] == "ok"
