"""Tests for user_repo module."""

import pytest
from user_repo import UserRepo, UserRepoError


def test_verify_correct_password():
    repo = UserRepo()
    result = repo.verify_password("admin", "secret")
    assert result["ok"] is True


def test_verify_wrong_password():
    repo = UserRepo()
    result = repo.verify_password("admin", "wrongpassword")
    assert result["ok"] is False


def test_verify_unknown_user():
    repo = UserRepo()
    with pytest.raises(UserRepoError) as exc:
        repo.verify_password("nonexistent", "password")
    assert "unknown_user" in str(exc.value)