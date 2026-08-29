"""The HTTP boundary both model providers share."""

import io
from urllib import error

import pytest

from richbuild._http import (
    MAX_RESPONSE_BYTES,
    HTTPResponse,
    UrllibTransport,
    bounded_read,
)


class _FakeResponse(io.BytesIO):
    """What urlopen yields: a file-like body with a status and headers."""

    def __init__(self, status, body, headers):
        super().__init__(body)
        self.status = status
        self.headers = headers

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def test_bounded_read_accepts_a_body_at_the_cap_and_refuses_one_byte_over():
    at_cap = b"x" * MAX_RESPONSE_BYTES

    assert bounded_read(io.BytesIO(at_cap)) == at_cap
    with pytest.raises(ValueError, match="too large"):
        bounded_read(io.BytesIO(at_cap + b"x"))


def test_a_response_is_a_status_in_range_and_a_bytes_body():
    assert HTTPResponse(200, b"{}").headers is None
    for status in (99, 600):
        with pytest.raises(ValueError):
            HTTPResponse(status, b"")
    with pytest.raises(TypeError):
        HTTPResponse(200, "text")


def test_the_transport_posts_compact_utf8_json_and_keeps_status_bytes_headers(
    monkeypatch,
):
    seen = {}

    def fake_urlopen(request, timeout):
        seen.update(
            url=request.full_url,
            method=request.get_method(),
            data=request.data,
            timeout=timeout,
        )
        return _FakeResponse(202, b'{"ok":true}', {"request-id": "abc"})

    monkeypatch.setattr("richbuild._http.urllib_request.urlopen", fake_urlopen)

    response = UrllibTransport().post_json(
        url="https://api.example.test/v1/messages",
        headers={"content-type": "application/json"},
        payload={"b": 1, "a": "é"},
        timeout_seconds=7.5,
    )

    assert response == HTTPResponse(202, b'{"ok":true}', {"request-id": "abc"})
    assert seen == {
        "url": "https://api.example.test/v1/messages",
        "method": "POST",
        "data": '{"b":1,"a":"é"}'.encode("utf-8"),
        "timeout": 7.5,
    }


def test_an_http_error_comes_back_as_a_response_rather_than_an_exception(
    monkeypatch,
):
    """The provider classifies the status itself; the body must never ride
    inside an exception where it could reach a log or a message."""

    def fake_urlopen(request, timeout):
        raise error.HTTPError(
            request.full_url,
            429,
            "Too Many Requests",
            {"retry-after": "1"},
            io.BytesIO(b'{"error":"slow down"}'),
        )

    monkeypatch.setattr("richbuild._http.urllib_request.urlopen", fake_urlopen)

    response = UrllibTransport().post_json(
        url="https://api.example.test/v1/messages",
        headers={},
        payload={},
        timeout_seconds=1.0,
    )

    assert response == HTTPResponse(429, b'{"error":"slow down"}', {"retry-after": "1"})
