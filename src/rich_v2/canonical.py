"""The one canonical JSON form.

Digests are identity here: an artifact is named by the SHA-256 of its bytes,
and an idempotency key by the hash of its request.  That only holds while every
producer of those bytes agrees on the encoding down to the byte, so this module
exists to be the single answer rather than the fourth one.

It replaces four separate definitions that had already drifted -- three agreed,
and the store's permitted NaN and Infinity, which ``json.dumps`` emits as bare
tokens that are not JSON at all and that no strict parser will read back.
"""

from __future__ import annotations

import json
from typing import Any


def canonical_json_text(value: Any) -> str:
    """Encode a value in the canonical form, as text."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_json_bytes(value: Any) -> bytes:
    """Encode a value in the canonical form, as newline-terminated UTF-8 bytes.

    The trailing newline is part of the encoding: artifact bytes have always
    carried it, and adding or removing one silently changes every digest.
    """

    return (canonical_json_text(value) + "\n").encode("utf-8")
