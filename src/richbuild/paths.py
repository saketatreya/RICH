"""One answer to "is this path safe, and whose is it?".

Five separate implementations of this check had grown up across the package at
four different strictnesses: the strictest rejected null bytes, backslashes,
over-long components and trailing slashes; another checked roughly half of
that; ``preview.py`` held two literal copies of one guard; and ``models.py``
added a round-trip normalization check none of the others made.

None of them was wrong on its own. The problem is that a path check is only as
good as the weakest place it is written down, and a fix applied to one of five
copies reaches none of the others.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Iterable, Sequence

# A single path component longer than this is refused by most filesystems, and
# a path that cannot be written is not one worth carrying further.
MAX_PATH_COMPONENT_BYTES = 255

_FORBIDDEN_PARTS = frozenset({"", ".", ".."})


class UnsafePath(ValueError):
    """A path is not a strict, relative, normalized POSIX path."""


def is_safe_relative_path(value: Any, *, max_bytes: int | None = None) -> bool:
    """Return whether a value is a strict relative POSIX path."""

    try:
        safe_relative_path(value, max_bytes=max_bytes)
    except UnsafePath:
        return False
    return True


def safe_relative_path(
    value: Any,
    *,
    max_bytes: int | None = None,
    label: str = "path",
) -> PurePosixPath:
    """Return ``value`` as a strict relative POSIX path, or raise ``UnsafePath``.

    Strict means every one of: text, non-empty, relative, POSIX separators
    only, no empty/dot/dot-dot component, no null byte, no trailing slash, no
    component over the filesystem limit, and already normalized -- so that the
    string that was checked is the string that gets used.
    """

    if not isinstance(value, str):
        raise UnsafePath(f"{label} must be a string")
    if not value:
        raise UnsafePath(f"{label} cannot be empty")
    if max_bytes is not None and len(value.encode("utf-8")) > max_bytes:
        raise UnsafePath(f"{label} exceeds {max_bytes} bytes: {value!r}")
    if value.startswith("/") or value.endswith("/"):
        raise UnsafePath(
            f"{label} must be a strict relative path with no trailing "
            f"slash: {value!r}"
        )
    if "\\" in value:
        raise UnsafePath(f"{label} must use POSIX '/' separators: {value!r}")
    if "\x00" in value:
        raise UnsafePath(f"{label} cannot contain a null byte")
    parts = value.split("/")
    if any(part in _FORBIDDEN_PARTS for part in parts):
        raise UnsafePath(f"{label} must be a strict relative path: {value!r}")
    if any(len(part.encode("utf-8")) > MAX_PATH_COMPONENT_BYTES for part in parts):
        raise UnsafePath(f"{label} has an oversized component: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts:
        raise UnsafePath(f"{label} must be relative: {value!r}")
    if str(path) != value:
        # The checked string and the used string have to be the same one.
        raise UnsafePath(f"{label} must be a strict relative path: {value!r}")
    return path


def is_owned(
    path: PurePosixPath | str,
    owned_paths: Sequence[str] | Iterable[PurePosixPath],
) -> bool:
    """Return whether ``path`` lies at or under one of ``owned_paths``.

    Ownership is by path component, never by string prefix: ``src/foo`` must
    not be read as owning ``src/foobar``.
    """

    candidate = PurePosixPath(path) if isinstance(path, str) else path
    for owner in owned_paths:
        root = PurePosixPath(owner) if isinstance(owner, str) else owner
        if candidate == root or root in candidate.parents:
            return True
    return False
