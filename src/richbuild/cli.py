"""Command-line surface for the approval-gated RICH local control plane."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import os
import shutil
import subprocess
import sys
from typing import Any, Sequence

from . import __version__
from .control_plane import ControlPlane
from .api import NO_MODEL_ROUTE, serve
from .executor import BubblewrapExecutor
from .execution import DefaultRunExecutor
from .preview import default_preview_orchestrator
from .runtime import CLAUDE_CODE_ROUTE, MODEL_ROUTES, default_interviewer
from .runlog import follow_run, run_is_settled
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
    parser.add_argument("--version", action="version", version=f"rich {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    def add_parser(name: str, **kwargs: object) -> argparse.ArgumentParser:
        return commands.add_parser(name, parents=[shared], **kwargs)

    add_parser("doctor", help="inspect required local execution capabilities")

    serve_command = add_parser("serve", help="serve the local versioned JSON API")
    serve_command.add_argument("--host", default="127.0.0.1")
    serve_command.add_argument("--port", type=int, default=8767)
    serve_command.add_argument(
        "--published-on-loopback",
        action="store_true",
        help=(
            "allow --host 0.0.0.0 inside a container whose port the runtime "
            "publishes only to the host's loopback (-p 127.0.0.1:8767:8767); "
            "the Host and Origin checks are enforced"
        ),
    )
    serve_command.add_argument(
        "--route",
        default=CLAUDE_CODE_ROUTE,
        choices=[*sorted(MODEL_ROUTES), NO_MODEL_ROUTE],
        help=(
            "how to reach the pinned model: an existing `claude` login, or "
            "ANTHROPIC_API_KEY. Never substituted for one another -- they "
            "spend different accounts (default: %(default)s)"
        ),
    )

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

    turn = add_parser(
        "interview-turn",
        help="one interview turn: say what you want; get questions back, or a draft specification",
    )
    turn.add_argument("project_id")
    turn.add_argument("message")
    turn.add_argument("--expected-draft-revision", type=int, default=0)
    turn.add_argument("--route", choices=sorted(MODEL_ROUTES), default=CLAUDE_CODE_ROUTE)

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
    push_repository = add_parser(
        "push-repository",
        help="push a succeeded run's verified release snapshot to a Git repository",
    )
    push_repository.add_argument("run_id")
    push_repository.add_argument(
        "remote", help="https://github.com/<owner>/<repo>.git or a file:// URL"
    )
    push_repository.add_argument("--branch", default="main")
    push_repository.add_argument(
        "--create", action="store_true", help="create the GitHub repository if missing"
    )
    push_repository.add_argument(
        "--public", action="store_true", help="create it public (default private)"
    )

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

    for name, help_text in (
        ("plan-change", "compute what moving between two approved revisions costs"),
        ("apply-change", "mark exactly the stale components stale, and nothing else"),
    ):
        change = add_parser(name, help=help_text)
        change.add_argument("--project", required=True)
        change.add_argument("--from-spec", required=True)
        change.add_argument("--to-spec", required=True)
        change.add_argument("--from-architecture", required=True)
        change.add_argument("--to-architecture", required=True)

    logs = add_parser(
        "logs", help="watch a run as a readable timeline of its durable events"
    )
    logs.add_argument("run_id")
    logs.add_argument("--after", type=int, default=0)
    logs.add_argument(
        "--follow",
        action="store_true",
        help="keep printing until the run settles",
    )

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
            serve(
                state_dir,
                host=args.host,
                port=args.port,
                route=args.route,
                published_on_loopback=args.published_on_loopback,
            )
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
        elif args.command == "interview-turn":
            # Its own control plane: the interviewer is built for the route the
            # person chose, and nothing else on this path needs a model.
            interviewing = ControlPlane(
                store, interviewer=default_interviewer(route=args.route)
            )
            _print_json(
                interviewing.interview_turn(
                    args.project_id,
                    message=args.message,
                    expected_draft_revision=args.expected_draft_revision,
                )
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
        elif args.command == "push-repository":
            _print_json(
                {
                    "push": control_plane.push_repository(
                        run_id=args.run_id,
                        remote=args.remote,
                        branch=args.branch,
                        create=args.create,
                        private=not args.public,
                    )
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
        elif args.command in {"plan-change", "apply-change"}:
            action = (
                control_plane.plan_change
                if args.command == "plan-change"
                else control_plane.apply_change
            )
            _print_json(
                action(
                    project_id=args.project,
                    from_spec_revision_id=args.from_spec,
                    to_spec_revision_id=args.to_spec,
                    from_architecture_revision_id=args.from_architecture,
                    to_architecture_revision_id=args.to_architecture,
                )
            )
        elif args.command == "logs":
            for line in follow_run(
                store,
                args.run_id,
                follow=args.follow,
                after_sequence=args.after,
                is_finished=lambda: run_is_settled(store, args.run_id),
            ):
                print(line, flush=True)
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
    """What this host can do, check by check, with the remedy for each miss.

    Required checks decide `ok`: a build needs Python, git, a sandbox the host
    permits, and the exact pinned toolchain. Model routes, preview tokens and
    the built canvas are reported with remedies but do not fail the host --
    a host with no login can still plan and interview deterministically.
    Secrets are reported as present or absent, never by value.
    """

    from .executor import (
        TRUSTED_NODE_VERSION,
        TRUSTED_PNPM_VERSION,
        SandboxUnavailable,
        sandbox_availability,
        trusted_node_pnpm_runtime,
    )
    from .api import canvas_origin

    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str, remedy: str = "", *, required: bool) -> None:
        entry: dict[str, Any] = {"name": name, "ok": ok, "detail": detail, "required": required}
        if not ok and remedy:
            entry["remedy"] = remedy
        checks.append(entry)

    check(
        "python", True, f"{sys.version.split()[0]} at {sys.executable}", required=True
    )
    git = shutil.which("git")
    check("git", bool(git), git or "not on PATH", "install git", required=True)

    sandbox_reason = sandbox_availability()
    check(
        "sandbox",
        sandbox_reason is None,
        "Bubblewrap runs with user namespaces" if sandbox_reason is None else sandbox_reason,
        "install bubblewrap and run RICH outside any container or sandbox that "
        "blocks unprivileged user namespaces; on Ubuntu 24.04 and later: "
        "sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0 (persist "
        "it in /etc/sysctl.d/); in Docker: --security-opt seccomp=unconfined "
        "--security-opt apparmor=unconfined",
        required=True,
    )

    def tool_version(name: str) -> str:
        path = shutil.which(name)
        if not path:
            return "not on PATH"
        try:
            out = subprocess.run(
                [path, "--version"], capture_output=True, text=True, timeout=10, check=False
            ).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            out = "?"
        return f"{out.lstrip('v')} at {path}"

    try:
        trusted_node_pnpm_runtime()
        check(
            "toolchain",
            True,
            f"node {TRUSTED_NODE_VERSION} and pnpm {TRUSTED_PNPM_VERSION}, identity-checked",
            required=True,
        )
    except SandboxUnavailable as exc:
        check(
            "toolchain",
            False,
            f"{exc} (node: {tool_version('node')}; pnpm: {tool_version('pnpm')})",
            f"install Node {TRUSTED_NODE_VERSION} exactly and pnpm {TRUSTED_PNPM_VERSION} "
            "via Corepack (`corepack prepare pnpm@" + TRUSTED_PNPM_VERSION + " --activate`); "
            "RICH never downloads a toolchain",
            required=True,
        )

    claude = shutil.which("claude")
    login = (Path.home() / ".claude" / ".credentials.json").is_file()
    check(
        "route claude-code",
        bool(claude) and login,
        (
            "claude is on PATH and logged in"
            if claude and login
            else ("claude is on PATH but not logged in" if claude else "claude is not on PATH")
        ),
        "install Claude Code and run `claude` once to log in, or use --route api",
        required=False,
    )
    check(
        "route api",
        bool(os.environ.get("ANTHROPIC_API_KEY")),
        "ANTHROPIC_API_KEY is set" if os.environ.get("ANTHROPIC_API_KEY") else "ANTHROPIC_API_KEY is not set",
        "export ANTHROPIC_API_KEY, or use --route claude-code",
        required=False,
    )
    for variable, what in (("NEON_API_TOKEN", "Neon"), ("VERCEL_TOKEN", "Vercel")):
        present = bool(os.environ.get(variable))
        check(
            f"preview {what.lower()}",
            present,
            f"{variable} is set" if present else f"{variable} is not set",
            f"export {variable} to deploy previews to {what}",
            required=False,
        )
    origin, canvas = canvas_origin()
    check(
        "canvas",
        origin != "missing",
        (
            f"{origin}: {canvas}"
            if origin != "missing"
            else f"not built at {canvas}"
        ),
        "npm --prefix web ci && npm --prefix web run build, or install the wheel "
        "from tools/build_wheel.py, which carries the canvas",
        required=False,
    )

    executor = BubblewrapExecutor()
    tools = {
        name: shutil.which(name)
        for name in ("python", "node", "pnpm", "npm", "git", "bwrap", "claude")
    }
    return {
        "ok": all(entry["ok"] for entry in checks if entry["required"]),
        "checks": checks,
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
