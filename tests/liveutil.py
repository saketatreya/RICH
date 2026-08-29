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
