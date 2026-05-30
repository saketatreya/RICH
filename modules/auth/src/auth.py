"""auth — authenticate users against user_repo, issue tokens via token_store.

Dependencies (user_repo, token_store) are received by INJECTION, not import.
This module does NOT import user_repo or token_store.
"""


class AuthError(Exception):
    """Errors raised by auth operations."""
    pass


def authenticate(username: str, password: str, *, user_repo, token_store) -> dict:
    """Authenticate a user and issue a token.

    Args:
        username: The user's name.
        password: The user's password.
        user_repo: Injected UserRepo instance (must implement verify_password).
        token_store: Injected TokenStore instance (must implement issue).

    Returns:
        {"token": str} on success.

    Raises:
        AuthError: invalid_credentials
    """
    try:
        result = user_repo.verify_password(username, password)
    except Exception:
        raise AuthError("invalid_credentials")

    if not result.get("ok"):
        raise AuthError("invalid_credentials")

    return token_store.issue(username)