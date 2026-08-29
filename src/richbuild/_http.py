"""The one HTTP boundary the model providers share.

Each provider keeps its own parsing, pricing and failure classification:
those are vendor-specific, and they are where the trust decisions live. The
transport is not. A bounded POST of a JSON body that comes back as status,
bytes and headers was byte-identical in both adapters, and a bound written
twice is a bound that drifts.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping, Protocol
from urllib import error, request as urllib_request

# A response larger than this is refused unread. Generous for a structured
# generation reply; a cap on what an adapter will buffer from the network.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class HTTPResponse:
    status_code: int
    body: bytes
    headers: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        if not 100 <= self.status_code <= 599:
            raise ValueError("HTTP status code is out of range")
        if not isinstance(self.body, bytes):
            raise TypeError("HTTP response body must be bytes")


class Transport(Protocol):
    """Trusted HTTP boundary, injectable for deterministic tests."""

    def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> HTTPResponse:
        ...


def bounded_read(response: Any) -> bytes:
    """Read a file-like response, refusing one over MAX_RESPONSE_BYTES."""

    body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        # This exception is sanitized by the provider's network boundary.
        raise ValueError("response too large")
    return body


class UrllibTransport:
    """POST one JSON body over urllib and return status, bytes and headers."""

    def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> HTTPResponse:
        encoded = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        http_request = urllib_request.Request(
            url=url,
            data=encoded,
            headers=dict(headers),
            method="POST",
        )
        try:
            with urllib_request.urlopen(
                http_request, timeout=timeout_seconds
            ) as response:
                body = bounded_read(response)
                return HTTPResponse(
                    status_code=int(response.status),
                    body=body,
                    headers=dict(response.headers.items()),
                )
        except error.HTTPError as exc:
            # HTTPError is also a file-like response.  Preserve only the status and
            # bytes for internal classification; its body never reaches an exception.
            body = bounded_read(exc)
            return HTTPResponse(
                status_code=int(exc.code),
                body=body,
                headers=dict(exc.headers.items()) if exc.headers else {},
            )
