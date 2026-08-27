"""Collecting the suite must never need a credential.

A test file that reads a key at import time turns "run the tests" into "have
an account first", and the failure looks like a broken repository rather than
a missing variable. The live marker exists so those tests skip; this checks
that the skipping happens before anything reaches for a secret.
"""

import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent

_CREDENTIALS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "NEON_API_TOKEN",
    "VERCEL_TOKEN",
)


def _offline_env() -> dict[str, str]:
    env = os.environ.copy()
    for name in _CREDENTIALS:
        env.pop(name, None)
    return env


def test_the_whole_suite_collects_with_no_credentials_present() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=REPO_ROOT,
        env=_offline_env(),
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Traceback" not in result.stderr


def test_live_tests_are_skipped_rather_than_failed_without_credentials() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-m", "live"],
        cwd=REPO_ROOT,
        env=_offline_env(),
        capture_output=True,
        text=True,
        timeout=180,
    )

    # No live test may run, and none may fail, when nothing was opted into.
    assert result.returncode in (0, 5), result.stdout + result.stderr
    assert " failed" not in result.stdout
