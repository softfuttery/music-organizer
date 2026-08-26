"""Password hashing and secret-file maintenance for the web control plane."""

from __future__ import annotations

import argparse
import getpass
import os
import secrets
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

HASH_PREFIXES = ("scrypt:", "pbkdf2:")


def is_password_hash(value: str) -> bool:
    return value.startswith(HASH_PREFIXES)


def hash_password(password: str) -> str:
    if not password:
        raise ValueError("password must not be empty")
    return generate_password_hash(password, method="scrypt")


def verify_password(stored_value: str, supplied_password: str) -> bool:
    if not stored_value:
        return not supplied_password
    if is_password_hash(stored_value):
        try:
            return check_password_hash(stored_value, supplied_password)
        except (ValueError, TypeError):
            return False
    # One-release compatibility for existing deployments. The maintenance CLI
    # converts this legacy plaintext value to scrypt before the new image starts.
    return secrets.compare_digest(stored_value, supplied_password)


def write_secret_bytes_atomic(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def write_secret_atomic(path: Path, value: str) -> None:
    write_secret_bytes_atomic(path, f"{value}\n".encode("utf-8"))


def set_password(path: Path) -> None:
    first = getpass.getpass("New administrator password: ")
    second = getpass.getpass("Confirm administrator password: ")
    if first != second:
        raise ValueError("passwords do not match")
    write_secret_atomic(path, hash_password(first))
    print(f"Password hash written to {path}")


def migrate_password(path: Path) -> bool:
    stored = path.read_text(encoding="utf-8").strip()
    if not stored:
        raise ValueError("password secret is empty")
    if is_password_hash(stored):
        print("Password secret is already hashed")
        return False
    write_secret_atomic(path, hash_password(stored))
    print("Legacy plaintext password migrated to scrypt")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage the web login password hash")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--set", dest="set_path", type=Path, metavar="PATH")
    action.add_argument("--migrate", dest="migrate_path", type=Path, metavar="PATH")
    args = parser.parse_args()
    try:
        if args.set_path:
            set_password(args.set_path)
        else:
            migrate_password(args.migrate_path)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
