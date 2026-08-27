"""Command-line surface for the approval-gated RICH local control plane."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import sys
from typing import Any, Sequence

from .control_plane import ControlPlane
from .api import serve
from .executor import BubblewrapExecutor
from .execution import DefaultRunExecutor
from .preview import default_preview_orchestrator
from .store import RichStore


def _parser() -> argparse.ArgumentParser:
    # --state-dir is accepted on both sides of the subcommand. Argparse puts a
    # top-level option before the subcommand only, and `rich serve --state-dir X`
    # is what a person actually types -- being told that is "unrecognized" is a
    # bad first minute with a tool.
    shared = argparse.ArgumentParser(add_help=False)
    # SUPPRESS, not None: the subparser shares this action, and a plain default
    # would overwrite a value given before the subcommand with nothing.
    shared.add_argument(
        "--state-dir",
        type=Path,
        default=argparse.SUPPRESS,
        help="durable local state directory (default: .rich/state)",
    )

    parser = argparse.ArgumentParser(
        prog="rich",
        description="RICH — an intent-to-verified-software compiler",
        parents=[shared],
    )
    commands = parser.add_subparsers(dest="command", required=True)

    def add_parser(name: str, **kwargs: object) -> argparse.ArgumentParser:
        return commands.add_parser(name, parents=[shared], **kwargs)

    add_parser("doctor", help="inspect required local execution capabilities")

    serve_command = add_parser("serve", help="serve the local versioned JSON API")
    serve_command.add_argument("--host", default="127.0.0.1")
    serve_command.add_argument("--port", type=int, default=8767)

    create = add_parser("project-create", help="create a durable project")
    create.add_argument("project_id")
    create.add_argument("name")

    show = add_parser("project-show", help="show durable project metadata")
    show.add_argument("project_id")

    interview = add_parser(
        "interview-submit",
        help="compile structured interview answers and request product-spec approval",
    )
    interview.add_argument("project_id")
    interview.add_argument("project_name")
    interview.add_argument("answers", type=Path)
    interview.add_argument("--expected-revision", type=int, required=True)

    approve = add_parser("approve", help="decide a requested approval gate")
    approve.add_argument("approval_id")
    approve.add_argument("--actor", required=True)
    approve.add_argument("--reason", default="")
    approve.add_argument("--reject", action="store_true")

    propose = add_parser(
        "architecture-propose",
        help="create the deterministic web baseline after product-spec approval",
    )
    propose.add_argument("project_id")
    propose.add_argument("spec_revision_id")
    propose.add_argument("spec_approval_id")
    propose.add_argument("--expected-revision", type=int, required=True)

    prepare = add_parser(
        "run-prepare",
        help="compile an approved architecture into durable tasks",
    )
    prepare.add_argument("architecture_approval_id")
    prepare.add_argument("budget", type=Path)

    scaffold = add_parser(
        "scaffold",
        help="materialize the approved run's target pack into an empty destination",
    )
    scaffold.add_argument("run_id")
    scaffold.add_argument("destination", type=Path)
    scaffold.add_argument("--package-scope")

    execute = add_parser(
        "run-execute",
        help="execute or resume a scaffolded run with the trusted runtime",
    )
    execute.add_argument("run_id")
    execute.add_argument("workspace", type=Path)
    execute.add_argument("--architecture-approval-id")

    preview_request = add_parser(
        "preview-request",
        help="request approval for a digest-bound Neon/Vercel preview",
    )
    preview_request.add_argument("run_id")
    preview_request.add_argument("source_dir", type=Path)
    preview_request.add_argument("neon_project_id")
    preview_request.add_argument("--expires-hours", type=int, default=24)
    preview_request.add_argument("--neon-branch-name")
    preview_request.add_argument("--neon-parent-branch-id")
    preview_request.add_argument("--vercel-project-id")
    preview_request.add_argument("--vercel-team-id")

    preview_deploy = add_parser(
        "preview-deploy",
        help="deploy an exact approved source digest to Neon and Vercel",
    )
    preview_deploy.add_argument("preview_id")
    preview_deploy.add_argument("approval_id")

    preview_destroy = add_parser(
        "preview-destroy",
        help="destroy a preview deployment and its database branch",
    )
    preview_destroy.add_argument("preview_id")

    preview_list = add_parser(
        "preview-list", help="list durable previews for a run"
    )
    preview_list.add_argument("run_id")


    rebuild = add_parser(
        "rebuild-node",
        help="forget one node's remembered generation so the next run redoes it",
    )
    rebuild.add_argument("--project", required=True)
    rebuild.add_argument("--node", required=True)
    rebuild.add_argument("--architecture-revision")

    cancel = add_parser(
        "cancel-run", help="ask a run to stop at its next checkpoint"
    )
    cancel.add_argument("run_id")
    cancel.add_argument("--reason", default="canceled by operator")

    events = add_parser("events", help="read durable run events")
    events.add_argument("run_id")
    events.add_argument("--after", type=int, default=0)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    state_dir = getattr(args, "state_dir", None) or Path(".rich/state")
    try:
        if args.command == "doctor":
            _print_json(_doctor())
            return 0
        if args.command == "serve":
            serve(state_dir, host=args.host, port=args.port)
            return 0

        store = RichStore(state_dir)
        # Deliberately unconfined. An operator at their own shell can
        # already write anywhere this process can, so a workspace_root here
        # would be friction wearing the costume of safety. The network surface
        # sets one because a network caller cannot be trusted with a path.
        control_plane = ControlPlane(
            store,
            preview_orchestrator=default_preview_orchestrator(),
            run_executor=DefaultRunExecutor(store),
        )
        if args.command == "project-create":
            _print_json(
                control_plane.create_project(
                    project_id=args.project_id, name=args.name
                )
            )
        elif args.command == "project-show":
            _print_json(store.get_project(args.project_id))
        elif args.command == "interview-submit":
            submission = control_plane.submit_interview(
                project_id=args.project_id,
                project_name=args.project_name,
                answers=_read_json(args.answers),
                expected_revision=args.expected_revision,
            )
            _print_json(
                {
                    "spec": submission.spec.to_dict(),
                    "revision": asdict(submission.revision),
                    "approval": submission.approval,
                }
            )
        elif args.command == "approve":
            _print_json(
                control_plane.decide_approval(
                    args.approval_id,
                    approved=not args.reject,
                    actor=args.actor,
                    reason=args.reason,
                )
            )
        elif args.command == "architecture-propose":
            submission = control_plane.propose_architecture(
                project_id=args.project_id,
                spec_revision_id=args.spec_revision_id,
                spec_approval_id=args.spec_approval_id,
                expected_revision=args.expected_revision,
            )
            _print_json(
                {
                    "architecture": submission.proposal.architecture.to_dict(),
                    "decisions": submission.proposal.decisions,
                    "risks": submission.proposal.risks,
                    "revision": asdict(submission.revision),
                    "approval": submission.approval,
                }
            )
        elif args.command == "run-prepare":
            prepared = control_plane.prepare_run(
                architecture_approval_id=args.architecture_approval_id,
                budget=_read_json(args.budget),
            )
            _print_json(
                {
                    "run": prepared.run,
                    "compiled": prepared.compiled.to_dict(),
                    "tasks": prepared.tasks,
                    "plan_artifact_digest": prepared.plan_artifact.digest,
                }
            )
        elif args.command == "scaffold":
            scaffolded = control_plane.scaffold_run(
                run_id=args.run_id,
                destination=args.destination,
                package_scope=args.package_scope,
            )
            _print_json(
                {
                    "destination": str(scaffolded.destination),
                    "manifest": scaffolded.manifest.as_dict(),
                    "manifest_artifact_digest": scaffolded.manifest_artifact.digest,
                }
            )
        elif args.command == "run-execute":
            report = control_plane.execute_run(
                run_id=args.run_id,
                workspace=args.workspace,
                architecture_approval_id=args.architecture_approval_id,
            )
            _print_json(
                {
                    "run_id": report.run_id,
                    "status": report.status,
                    "succeeded": report.succeeded,
                    "task_statuses": dict(report.task_statuses),
                    "task_attempts": dict(report.task_attempts),
                }
            )
        elif args.command == "preview-request":
            if args.expires_hours < 1 or args.expires_hours > 720:
                raise ValueError("--expires-hours must be between 1 and 720")
            submitted = control_plane.request_preview(
                run_id=args.run_id,
                source_dir=args.source_dir,
                neon_project_id=args.neon_project_id,
                expires_at=(
                    datetime.now(timezone.utc)
                    + timedelta(hours=args.expires_hours)
                ),
                neon_branch_name=args.neon_branch_name,
                neon_parent_branch_id=args.neon_parent_branch_id,
                vercel_project_id=args.vercel_project_id,
                vercel_team_id=args.vercel_team_id,
            )
            _print_json(
                {
                    "preview": submitted.preview,
                    "approval": submitted.approval,
                }
            )
        elif args.command == "preview-deploy":
            deployed = control_plane.deploy_preview(
                preview_id=args.preview_id,
                approval_id=args.approval_id,
            )
            _print_json(
                {
                    "preview": deployed.preview,
                    "result": asdict(deployed.result),
                }
            )
        elif args.command == "preview-destroy":
            _print_json(
                {
                    "preview": control_plane.destroy_preview(
                        preview_id=args.preview_id
                    )
                }
            )
        elif args.command == "preview-list":
            _print_json(
                {"previews": store.list_previews(args.run_id)}
            )
        elif args.command == "rebuild-node":
            _print_json(
                control_plane.rebuild_node(
                    project_id=args.project,
                    node_id=args.node,
                    architecture_revision_id=args.architecture_revision,
                )
            )
        elif args.command == "cancel-run":
            _print_json(
                control_plane.cancel_run(run_id=args.run_id, reason=args.reason)
            )
        elif args.command == "events":
            _print_json(
                {
                    "events": store.list_events(
                        args.run_id, after_sequence=args.after
                    )
                }
            )
        else:  # pragma: no cover - argparse guarantees a known command.
            raise AssertionError(args.command)
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {"error": type(exc).__name__, "message": str(exc)},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


def _doctor() -> dict[str, Any]:
    executor = BubblewrapExecutor()
    tools = {
        name: shutil.which(name)
        for name in ("python", "node", "pnpm", "npm", "git", "bwrap")
    }
    return {
        "ok": executor.available() and bool(tools["python"]) and bool(tools["git"]),
        "sandbox": {
            "provider": "bubblewrap",
            "available": executor.available(),
            "network_default": "denied",
            "unsafe_fallback": False,
        },
        "tools": tools,
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
