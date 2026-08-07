"""Regression tests for credential-free test discovery."""

import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def _offline_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("OPENROUTER_API_KEY", None)
    return env


def test_harness_import_does_not_require_live_credentials() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import runpy; runpy.run_path('tests/test_harness.py')",
        ],
        cwd=REPO_ROOT,
        env=_offline_env(),
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr


def test_live_runner_fails_cleanly_without_credentials() -> None:
    result = subprocess.run(
        [sys.executable, "tests/run_tests.py"],
        cwd=REPO_ROOT,
        env=_offline_env(),
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 2
    assert "OPENROUTER_API_KEY not set" in result.stdout
    assert "Traceback" not in result.stderr
