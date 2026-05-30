"""user_repo — password verification with constant-time comparison."""

import hashlib
import hmac


# Pre-seeded test user: username="admin", password="secret"
_TEST_USER = "admin"
_TEST_HASH = hashlib.sha256(b"secret:fixed-salt-for-testing").hexdigest()


class UserRepo:
    """User repository. Stores password hashes, verifies with constant-time comparison."""

    def __init__(self):
        self._users: dict[str, str] = {
            _TEST_USER: _TEST_HASH,
        }

    def verify_password(self, username: str, password: str) -> dict:
        """Verify a password for a user. Constant-time comparison over stored hash.

        Returns:
            {"ok": True} if credentials match.
            {"ok": False} if credentials don't match.

        Raises:
            UserRepoError: unknown_user
        """
        stored_hash = self._users.get(username)
        if stored_hash is None:
            raise UserRepoError("unknown_user")

        computed = hashlib.sha256(
            f"{password}:fixed-salt-for-testing".encode()
        ).hexdigest()

        # Constant-time comparison
        ok = hmac.compare_digest(computed, stored_hash)
        return {"ok": ok}


class UserRepoError(Exception):
    """Errors raised by user_repo operations."""
    pass