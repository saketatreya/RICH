"""token_store — in-memory token store with 24h expiry."""

import secrets
import time
from dataclasses import dataclass, field


@dataclass
class TokenRecord:
    token: str
    subject: str
    issued_at: float = field(default_factory=time.time)


class TokenStore:
    """In-memory token store. Tokens valid for 24 hours from issue."""

    def __init__(self, ttl_seconds: int = 86400):
        self._store: dict[str, TokenRecord] = {}
        self._ttl = ttl_seconds

    def issue(self, subject: str) -> dict:
        """Issue a new token for the given subject."""
        token = secrets.token_hex(32)
        record = TokenRecord(token=token, subject=subject)
        self._store[token] = record
        return {"token": token}

    def validate(self, token: str) -> dict:
        """Validate a token. Returns the subject on success.

        Raises:
            TokenStoreError: invalid_token or expired_token
        """
        record = self._store.get(token)
        if record is None:
            raise TokenStoreError("invalid_token")
        now = time.time()
        if now - record.issued_at > self._ttl:
            raise TokenStoreError("expired_token")
        return {"subject": record.subject}


class TokenStoreError(Exception):
    """Errors raised by token_store operations."""
    pass
