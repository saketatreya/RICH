"""A real preview: Neon branch, Vercel deployment, a URL that answers, torn down.

Opt-in (`--run-live`) and self-skipping: it needs NEON_API_TOKEN, VERCEL_TOKEN
and RICH_NEON_PROJECT_ID, and it spends a Neon branch and a Vercel deployment
for a few minutes.  The run it deploys is the seeded, scaffolded application
the M6 drive uses -- deployment is what is under test here, not generation.
"""

from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
from urllib.request import urlopen

import pytest

from liveutil import require_preview_credentials

from richbuild.control_plane import ControlPlane
from richbuild.preview import default_preview_orchestrator
from richbuild.store import RichStore

pytestmark = pytest.mark.live


def _seeder():
    path = Path(__file__).resolve().parents[1] / "web" / "drive" / "seed-release.py"
    spec = importlib.util.spec_from_file_location("seed_release", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_a_preview_deploys_answers_and_is_torn_down(tmp_path):
    coordinates = require_preview_credentials()
    root = tmp_path / "drive-preview"
    assert _seeder().main(["seed-release.py", str(root)]) == 0
    control_plane = ControlPlane(
        RichStore(root / "state"), preview_orchestrator=default_preview_orchestrator()
    )
    project = control_plane.store.get_project("project.drive-m6")
    run = control_plane.store.list_runs(project["id"])[-1]
    assert run["status"] == "succeeded"

    submission = control_plane.request_preview(
        run_id=run["id"],
        source_dir=root / "generated",
        neon_project_id=coordinates["neon_project_id"],
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        vercel_project_id=coordinates["vercel_project_id"],
        vercel_team_id=coordinates["vercel_team_id"],
    )
    control_plane.decide_approval(
        submission.approval["id"], approved=True, actor="live-test"
    )
    preview_id = submission.preview["id"]
    try:
        deployed = control_plane.deploy_preview(
            preview_id=preview_id, approval_id=submission.approval["id"]
        )
        url = deployed.result.preview_url
        assert url.startswith("https://")
        with urlopen(url, timeout=120) as response:
            assert response.status == 200
            assert b"<html" in response.read(4096).lower()
        with urlopen(f"{url.rstrip('/')}/api/health", timeout=60) as response:
            assert response.status == 200
    finally:
        destroyed = control_plane.destroy_preview(preview_id=preview_id)
        assert destroyed["status"] in {"destroyed", "destroy_failed"}
    assert control_plane.store.get_preview(preview_id)["status"] == "destroyed"
