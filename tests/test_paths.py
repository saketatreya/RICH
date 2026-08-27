"""One path check, pinned — because a path guard is only as good as the weakest
place it is written down."""

from pathlib import PurePosixPath

import pytest

from richbuild.paths import (
    UnsafePath,
    is_owned,
    is_safe_relative_path,
    safe_relative_path,
)


@pytest.mark.parametrize(
    "value",
    [
        "apps/web/page.tsx",
        "a",
        "packages/domain/src/operations.ts",
    ],
)
def test_ordinary_relative_paths_are_accepted(value):
    assert safe_relative_path(value) == PurePosixPath(value)
    assert is_safe_relative_path(value)


@pytest.mark.parametrize(
    "value,reason",
    [
        ("/etc/passwd", "absolute"),
        ("../outside.ts", "dot-dot escapes"),
        ("apps/../../etc/passwd", "dot-dot anywhere"),
        ("apps/./web.ts", "single dot is not normalized"),
        ("apps//web.ts", "empty component"),
        ("apps/web/", "trailing slash"),
        ("apps\\web.ts", "backslash separator"),
        ("apps/web\x00.ts", "null byte"),
        ("", "empty"),
        (".", "the current directory is not a file"),
        ("..", "the parent directory is not a file"),
    ],
)
def test_every_escape_shape_is_refused(value, reason):
    with pytest.raises(UnsafePath):
        safe_relative_path(value), reason
    assert not is_safe_relative_path(value)


def test_non_strings_and_oversized_components_are_refused():
    for value in (None, 7, b"apps/web.ts", PurePosixPath("apps/web.ts")):
        with pytest.raises(UnsafePath):
            safe_relative_path(value)
    with pytest.raises(UnsafePath, match="oversized component"):
        safe_relative_path("apps/" + "a" * 256)
    with pytest.raises(UnsafePath, match="exceeds"):
        safe_relative_path("apps/web.ts", max_bytes=4)


def test_ownership_is_by_component_never_by_string_prefix():
    """src/foo must not be read as owning src/foobar."""

    assert is_owned("src/foo/a.ts", ["src/foo"])
    assert is_owned("src/foo", ["src/foo"])
    assert not is_owned("src/foobar/a.ts", ["src/foo"])
    assert not is_owned("src/fo/a.ts", ["src/foo"])
    assert not is_owned("other/a.ts", ["src/foo"])


def test_ownership_accepts_either_spelling_of_its_arguments():
    """The two implementations this replaced took incompatible types."""

    assert is_owned(PurePosixPath("src/foo/a.ts"), ["src/foo"])
    assert is_owned("src/foo/a.ts", [PurePosixPath("src/foo")])
    assert not is_owned(PurePosixPath("src/foobar/a.ts"), [PurePosixPath("src/foo")])
