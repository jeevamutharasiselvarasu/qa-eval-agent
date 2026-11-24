"""Encryption for secrets stored in the app database (e.g. the Jira API token entered on the Settings page).

Key source, in order: QA_SECRET_KEY (a Fernet key) or a key file created on first use at
<QA_STORAGE_DIR>/.secret.key with owner-only permissions. Losing the key file means stored secrets must be re-entered.
Secrets are decrypted only inside the backend and are never returned by the API.
"""
from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from ..config import get_settings


def _key() -> bytes:
    env = os.getenv("QA_SECRET_KEY", "").strip()
    if env:
        return env.encode()
    path = Path(get_settings().storage_dir) / ".secret.key"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(Fernet.generate_key())
    return path.read_bytes().strip()


def encrypt(value: str) -> str:
    return Fernet(_key()).encrypt(value.encode()).decode()


def decrypt(token: str) -> str | None:
    try:
        return Fernet(_key()).decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return None
