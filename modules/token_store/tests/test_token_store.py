"""Tests for token_store module."""

import time
import pytest
from token_store import TokenStore, TokenStoreError


def test_issue_returns_unique_token():
    ts = TokenStore()
    r1 = ts.issue("alice")
    r2 = ts.issue("bob")
    assert r1["token"] != r2["token"]


def test_validate_returns_subject():
    ts = TokenStore()
    r = ts.issue("alice")
    result = ts.validate(r["token"])
    assert result["subject"] == "alice"


def test_validate_invalid_token():
    ts = TokenStore()
    with pytest.raises(TokenStoreError) as exc:
        ts.validate("nonexistent-token")
    assert "invalid_token" in str(exc.value)


def test_validate_expired_token():
    ts = TokenStore(ttl_seconds=0)  # immediate expiry
    r = ts.issue("alice")
    with pytest.raises(TokenStoreError) as exc:
        ts.validate(r["token"])
    assert "expired_token" in str(exc.value)
