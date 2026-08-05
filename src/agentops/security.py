from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from .database import Database

SENSITIVE_KEYS = {"authorization", "api_key", "apikey", "password", "secret", "token"}


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if key.lower() in SENSITIVE_KEYS else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


@dataclass(frozen=True)
class Actor:
    name: str
    role: str


class Authenticator:
    def __init__(self, database: Database):
        self.db = database

    def enabled(self) -> bool:
        if os.getenv("AGENTOPS_API_KEY"):
            return True
        with self.db.connect() as connection:
            return connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0

    def authenticate(self, token: str) -> Actor | None:
        bootstrap = os.getenv("AGENTOPS_API_KEY")
        if bootstrap and hmac.compare_digest(token, bootstrap):
            return Actor("bootstrap-admin", "admin")
        digest = token_hash(token)
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT name,role,token_hash FROM users WHERE token_hash=?", (digest,)
            ).fetchone()
        if row is not None and hmac.compare_digest(digest, row["token_hash"]):
            return Actor(row["name"], row["role"])
        return None


class SecretVault:
    def __init__(self):
        configured = os.getenv("AGENTOPS_ENCRYPTION_KEY")
        self._fernet = None
        if configured:
            key = base64.urlsafe_b64encode(hashlib.sha256(configured.encode()).digest())
            self._fernet = Fernet(key)

    @property
    def configured(self) -> bool:
        return self._fernet is not None

    def encrypt(self, value: str) -> str:
        if self._fernet is None:
            raise RuntimeError("AGENTOPS_ENCRYPTION_KEY is not configured")
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        if self._fernet is None:
            raise RuntimeError("AGENTOPS_ENCRYPTION_KEY is not configured")
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken as error:
            raise RuntimeError("secret cannot be decrypted with the configured key") from error
