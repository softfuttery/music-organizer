# Runtime secrets

This directory is intentionally excluded from Git and the Docker build context.
Create these files on the deployment host with mode 0600:

- `auth_password`: Werkzeug scrypt hash for the administrator login password.
- `flask_secret_key`: random Flask session signing key.
- `magicpush/token`: MagicPush Bearer token. The web service mounts this
  directory read-write so the configuration page can replace the token
  atomically; the worker mounts it read-only.

The Compose file mounts authentication secrets read-only under `/run/secrets`. Create or replace
the password hash with `python -m music_organizer.auth --set ...`; do not write
the plaintext password or a reversible Base64 value to this directory. Never
pass either secret as a Docker build argument or environment variable.
