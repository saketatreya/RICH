"""Seed a throwaway state directory with a project whose run has succeeded.

For the M6 drive only.  The run is marked succeeded WITHOUT verification --
no gate ran -- so this must never touch a real state directory: it refuses
any path that does not contain "drive".  Everything else goes through the
control plane exactly as a customer's would: interview, approvals, the
planner's architecture, a prepared and scaffolded run.  The release snapshot
is the scaffolded tree, which is what the ZIP download and the repository
push hand out.

    python web/drive/seed-release.py .rich/drive-m6
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from richbuild.control_plane import ControlPlane  # noqa: E402
from richbuild.preview import create_deployment_snapshot  # noqa: E402
from richbuild.store import RichStore  # noqa: E402

ANSWERS = {
    "goal": "A persistent todo application for signed-in teams",
    "audiences": ["technical founders"],
    "roles": ["Members manage tasks only in their own team."],
    "capabilities": [
        {
            "id": "req.todo",
            "title": "Manage todos",
            "statement": "A member adds a task that remains after refresh.",
        }
    ],
    "data_policy": ["Tasks remain until explicitly deleted."],
    "quality_constraints": [
        {
            "id": "req.a11y",
            "title": "Keyboard access",
            "statement": "All todo actions are keyboard accessible.",
        }
    ],
    "scenarios": [
        {
            "id": "scenario.todo",
            "title": "Add todo",
            "when": ["A member adds Buy milk."],
            "then": ["Buy milk remains after refresh."],
            "requirement_ids": ["req.todo"],
            "oracle": [
                {"action": "open_requirement"},
                {"action": "fill", "locator": {"kind": "label", "value": "New task"}, "value": "Buy milk"},
                {"action": "click", "locator": {"kind": "role", "value": "button", "name": "Add task"}},
                {"action": "reload"},
                {"action": "assert_visible", "locator": {"kind": "text", "value": "Buy milk"}},
            ],
        },
        {
            "id": "scenario.a11y",
            "title": "Keyboard todo",
            "when": ["A member uses only the keyboard."],
            "then": ["They can add and complete a todo."],
            "requirement_ids": ["req.a11y"],
            "oracle": [
                {"action": "open_requirement"},
                {"action": "keyboard", "value": "Tab"},
                {"action": "assert_visible", "locator": {"kind": "role", "value": "textbox"}},
            ],
        },
    ],
}

BUDGET = {
    "max_model_attempts": 20,
    "max_input_tokens": 320_000,
    "max_output_tokens": 160_000,
    "max_cost_usd": "10.00",
    "max_execution_seconds": 2_400,
}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    root = Path(argv[1]).resolve()
    if "drive" not in root.as_posix():
        print("refusing: the seeded run is unverified; use a directory with 'drive' in its path")
        return 2
    root.mkdir(parents=True, exist_ok=True)
    control_plane = ControlPlane(RichStore(root / "state"))
    project = control_plane.create_project(project_id="project.drive-m6", name="Drive M6")
    spec = control_plane.submit_interview(
        project_id=project["id"], project_name=project["name"], answers=ANSWERS, expected_revision=0
    )
    control_plane.decide_approval(spec.approval["id"], approved=True, actor="founder")
    architecture = control_plane.propose_architecture(
        project_id=project["id"],
        spec_revision_id=spec.revision.id,
        spec_approval_id=spec.approval["id"],
        expected_revision=1,
    )
    control_plane.decide_approval(architecture.approval["id"], approved=True, actor="founder")
    prepared = control_plane.prepare_run(
        architecture_approval_id=architecture.approval["id"], budget=BUDGET
    )
    run_id = prepared.run["id"]
    destination = root / "generated"
    control_plane.scaffold_run(run_id=run_id, destination=destination)
    store = control_plane.store
    store.set_run_status(run_id, "running", expected_status="ready")
    store.set_run_status(run_id, "verifying", expected_status="running")
    store.set_run_status(run_id, "succeeded", expected_status="verifying")
    store.append_event(run_id, "run.execution_finished", {"status": "succeeded", "seeded": True})
    snapshot = store.put_artifact(
        create_deployment_snapshot(destination),
        media_type="application/vnd.rich.release-source+zip",
    )
    store.attach_artifact(run_id, snapshot.digest, role="source:release-snapshot")
    bare = root / "remote.git"
    if not bare.exists():
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    seed = {
        "project_id": project["id"],
        "run_id": run_id,
        "release_digest": snapshot.digest,
        "bare": str(bare),
        "remote": bare.as_uri(),
    }
    (root / "seed.json").write_text(json.dumps(seed, indent=2) + "\n")
    print(json.dumps(seed, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
