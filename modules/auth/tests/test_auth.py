"""Tests for auth module using FAKES for dependencies.

Critical: these tests never import the real user_repo or token_store.
They use fakes that satisfy the dependency contracts.
"""

import pytest
from auth import authenticate, AuthError


class FakeUserRepo:
    """Fake that satisfies the user_repo contract."""
    def __init__(self, password_map=None):
        self._users = password_map or {"admin": "secret"}
        self._verify_calls = []

    def verify_password(self, username: str, password: str):
        self._verify_calls.append((username, password))
        if username not in self._users:
            raise Exception("unknown_user")
        ok = self._users[username] == password
        return {"ok": ok}


class FakeTokenStore:
    """Fake that satisfies the token_store contract."""
    def __init__(self):
        self._issued = []
        self._counter = 0

    def issue(self, subject: str):
        self._counter += 1
        token = f"fake-token-{self._counter}-{subject}"
        self._issued.append({"token": token, "subject": subject})
        return {"token": token}

    def validate(self, token: str):
        for record in self._issued:
            if record["token"] == token:
                return {"subject": record["subject"]}
        raise Exception("invalid_token")


def test_authenticate_success():
    """Valid credentials → returns a token."""
    user_repo = FakeUserRepo({"alice": "pass123"})
    token_store = FakeTokenStore()

    result = authenticate("alice", "pass123",
                          user_repo=user_repo, token_store=token_store)

    assert "token" in result
    assert result["token"].startswith("fake-token-")
    assert token_store.validate(result["token"])["subject"] == "alice"


def test_authenticate_wrong_password():
    """Wrong password → invalid_credentials."""
    user_repo = FakeUserRepo({"alice": "pass123"})
    token_store = FakeTokenStore()

    with pytest.raises(AuthError) as exc:
        authenticate("alice", "wrong",
                     user_repo=user_repo, token_store=token_store)
    assert "invalid_credentials" in str(exc.value)


def test_authenticate_unknown_user():
    """Unknown user → invalid_credentials."""
    user_repo = FakeUserRepo({"alice": "pass123"})
    token_store = FakeTokenStore()

    with pytest.raises(AuthError) as exc:
        authenticate("bob", "whatever",
                     user_repo=user_repo, token_store=token_store)
    assert "invalid_credentials" in str(exc.value)


def test_authenticate_does_not_import_deps():
    """Verify auth module does not import user_repo or token_store directly."""
    import auth as auth_mod
    import sys

    # The auth module should not have user_repo or token_store in its namespace
    assert "user_repo" not in dir(auth_mod), \
        "auth module imports user_repo — violates D4 (injection only)"
    assert "token_store" not in dir(auth_mod), \
        "auth module imports token_store — violates D4 (injection only)"

    # Also check that the source doesn't contain actual import statements
    auth_source = open(auth_mod.__file__).read()
    import re
    import_pat = re.compile(
        r'^\s*(?:import\s+user_repo|import\s+token_store|'
        r'from\s+user_repo|from\s+token_store)',
        re.MULTILINE
    )
    assert not import_pat.search(auth_source), \
        "auth source imports user_repo or token_store — violates D4"