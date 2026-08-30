"""What a live test needs before it may spend anything.

Four suites carried the same two skip checks by hand. One place, one message.
"""

from pathlib import Path
import shutil

import pytest


def require_claude_login() -> None:
    """Skip unless the `claude` CLI is installed and logged in.

    The claude-code route spends an existing login; a test that ran without one
    would fail on the credential rather than on what it is testing.
    """

    if shutil.which("claude") is None:
        pytest.skip("live test; the `claude` CLI is not on PATH")
    if not (Path.home() / ".claude" / ".credentials.json").exists():
        pytest.skip("live test; run `claude` once to log in first")


def require_preview_credentials() -> dict[str, str | None]:
    """Skip unless Neon and Vercel can be reached with real credentials.

    Returns the coordinates a live preview needs.  The project ids are not
    secrets, but they are the operator's: a test cannot invent them.
    """

    import os

    missing = [
        name
        for name in ("NEON_API_TOKEN", "VERCEL_TOKEN", "RICH_NEON_PROJECT_ID")
        if not os.environ.get(name)
    ]
    if missing:
        pytest.skip(
            "live test; set NEON_API_TOKEN, VERCEL_TOKEN and RICH_NEON_PROJECT_ID "
            f"(missing: {', '.join(missing)})"
        )
    return {
        "neon_project_id": os.environ["RICH_NEON_PROJECT_ID"],
        "vercel_project_id": os.environ.get("RICH_VERCEL_PROJECT_ID") or None,
        "vercel_team_id": os.environ.get("RICH_VERCEL_TEAM_ID") or None,
    }


def live_cache_root() -> Path | None:
    """Where live tests may share the pnpm store and the browsers, if anywhere.

    A cold bootstrap downloads about 2 GiB. ``--basetemp`` is wiped at the
    start of every run, so a cache that survives between runs has to live
    elsewhere and be asked for explicitly: ``RICH_LIVE_CACHE_ROOT``. Unset,
    every live workspace installs into itself, as before. Set, the bootstrap
    writes the cache under a lock and every gate mounts it read-only -- the
    same trust rule the product applies to ``<state>/../cache``.
    """

    import os

    value = os.environ.get("RICH_LIVE_CACHE_ROOT")
    if not value:
        return None
    root = Path(value).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root
