import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from music_organizer.auth import (
    hash_password,
    is_password_hash,
    migrate_password,
    verify_password,
    write_secret_atomic,
)


class PasswordSecretTests(unittest.TestCase):
    def test_atomic_secret_write_requests_restricted_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "secret"
            with (
                mock.patch("music_organizer.auth.os.open", wraps=os.open) as opened,
                mock.patch("music_organizer.auth.os.chmod", wraps=os.chmod) as chmod,
            ):
                write_secret_atomic(path, "sensitive-value")

            self.assertEqual(
                path.read_text(encoding="utf-8").strip(), "sensitive-value"
            )
            self.assertEqual(opened.call_args.args[2], 0o600)
            chmod.assert_called_once_with(path, 0o600)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_scrypt_hash_round_trip(self):
        stored = hash_password("correct horse battery staple")

        self.assertTrue(is_password_hash(stored))
        self.assertNotIn("correct horse battery staple", stored)
        self.assertTrue(verify_password(stored, "correct horse battery staple"))
        self.assertFalse(verify_password(stored, "wrong"))

    def test_legacy_secret_is_migrated_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth_password"
            path.write_text("legacy-password\n", encoding="utf-8")

            self.assertTrue(migrate_password(path))
            stored = path.read_text(encoding="utf-8").strip()
            self.assertTrue(stored.startswith("scrypt:"))
            self.assertNotIn("legacy-password", stored)
            self.assertTrue(verify_password(stored, "legacy-password"))
            self.assertFalse(migrate_password(path))


if __name__ == "__main__":
    unittest.main()
