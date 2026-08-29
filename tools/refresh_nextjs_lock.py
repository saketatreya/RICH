#!/usr/bin/env python3
"""Regenerate the Next.js target pack's pinned pnpm lock from the pack itself.

``src/richbuild/target_packs/_nextjs_lock.py`` is ``pnpm-lock.yaml`` for the largest
scaffold the pack can render -- every optional workspace importer present, every
edge between them, the ``@rich-template`` scope standing in for the project's --
and the pack cuts smaller scaffolds' locks out of it. Until this tool existed the
module's docstring was its only provenance. Now the provenance is executable:

    python tools/refresh_nextjs_lock.py                 # regenerate the module
    python tools/refresh_nextjs_lock.py --check         # exit 1 if it is stale
    python tools/refresh_nextjs_lock.py --node-root DIR # a Node 22.22.3 not on PATH

It renders that largest scaffold under ``.rich/lock-refresh/`` (gitignored, on real
disk, never /tmp), runs the operator's pinned pnpm 10.34.5 -- resolved from the
Corepack cache exactly the way ``richbuild.executor.trusted_node_pnpm_runtime``
resolves it, with the same exact-version checks, and never downloaded -- as
``install --lockfile-only --ignore-scripts --strict-peer-dependencies`` over the
lock the pack currently renders, so unchanged pins keep their resolutions and only
changed ones are resolved afresh, and rewrites the module verbatim, keeping its
docstring.

This is a developer tool, not the product. It reaches the npm registry over the
network on purpose; nothing in ``richbuild`` may do that while scaffolding or
verifying, and the one network-enabled step the product has -- the sandboxed
bootstrap -- installs from this lock, frozen. Run it when a pinned dependency
changes and commit the regenerated module in the same change. It reads the
operator's ``~/.npmrc`` like any pnpm invocation, so a registry mirror configured
there shapes the result; pnpm's own cache, state and store are kept in the scratch
directory rather than the operator's home.
"""

from __future__ import annotations

import argparse
import difflib
import os
from pathlib import Path
import shutil
import subprocess
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from richbuild.executor import SandboxUnavailable, trusted_node_pnpm_runtime  # noqa: E402
from richbuild.models import (  # noqa: E402
    AcceptanceScenario,
    NodeKind,
    ProjectSpec,
    Requirement,
)
from richbuild.planner import plan_nextjs_architecture  # noqa: E402
from richbuild.target_packs import nextjs  # noqa: E402

LOCK_MODULE = ROOT / "src" / "richbuild" / "target_packs" / "_nextjs_lock.py"
SCRATCH = ROOT / ".rich" / "lock-refresh"
TEMPLATE_MARKER = 'PNPM_LOCK_TEMPLATE = """'


def largest_scaffold() -> nextjs.NextJsTargetPack:
    """The scaffold whose lock is the template: every optional importer present.

    The deterministic planner emits the data and adapter nodes from the words
    the intent uses, so the intent here says both. The check below is what
    makes that an invariant rather than a hope: a planner whose vocabulary
    drifted would produce a smaller scaffold, and a lock cut from a smaller
    scaffold cannot serve a larger one.
    """

    statement = (
        "An operator persists an approved record and an external provider is "
        "notified."
    )
    project = ProjectSpec(
        id="project.rich-template",
        name="RICH template",
        goal="Persist approved records in a database and notify an external provider.",
        audiences=("operators",),
        requirements=(
            Requirement(id="req.records", title="Store records", statement=statement),
        ),
        acceptance_scenarios=(
            AcceptanceScenario(
                id="scenario.records",
                title="Record persists",
                given=("The application is available.",),
                when=("An operator stores a record.",),
                then=("The record remains available.",),
                requirement_ids=("req.records",),
                oracle=(
                    {"action": "open_requirement"},
                    {
                        "action": "assert_visible",
                        "locator": {"kind": "text", "value": statement},
                    },
                ),
            ),
        ),
    )
    architecture = plan_nextjs_architecture(project).architecture
    kinds = {node.kind for node in architecture.nodes}
    if not {NodeKind.DATA, NodeKind.ADAPTER} <= kinds:
        raise SystemExit(
            "the planner did not emit both optional nodes for the template "
            "intent; the lock must be cut from the largest scaffold"
        )
    return nextjs.NextJsTargetPack(
        nextjs.NextJsTargetPackConfig(
            project_name="rich-template",
            package_scope=nextjs._LOCK_SCOPE_PLACEHOLDER,
            project_spec=project,
            architecture=architecture,
        )
    )


def pinned_toolchain(node_root: str | None) -> tuple[Path, Path]:
    """Host paths of the exact Node and pnpm the sandbox would mount."""

    try:
        runtime = trusted_node_pnpm_runtime(node_root=node_root)
    except SandboxUnavailable as exc:
        raise SystemExit(
            f"pinned toolchain unavailable: {exc}\n"
            "Put Node 22.22.3 first on PATH or pass --node-root; pnpm 10.34.5 "
            "must already be in the Corepack cache. This tool downloads nothing."
        ) from exc
    mounts = {
        mount.guest_path: Path(mount.host_path)
        for mount in runtime.executor.tool_mounts
    }
    return (
        mounts["/opt/rich-tools/node"] / "bin" / "node",
        mounts["/opt/rich-tools/pnpm"] / "bin" / "pnpm.cjs",
    )


def resolve_lock(node_root: str | None) -> str:
    """Render the largest scaffold and let pinned pnpm resolve its lock."""

    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)
    workspace = SCRATCH / "workspace"
    largest_scaffold().scaffold(workspace)
    node, pnpm = pinned_toolchain(node_root)
    for name in ("cache", "state", "data", "config"):
        (SCRATCH / name).mkdir(parents=True, exist_ok=True)
    environment = {
        **os.environ,
        "CI": "1",
        "XDG_CACHE_HOME": str(SCRATCH / "cache"),
        "XDG_STATE_HOME": str(SCRATCH / "state"),
        "XDG_DATA_HOME": str(SCRATCH / "data"),
        "XDG_CONFIG_HOME": str(SCRATCH / "config"),
    }
    argv = [
        str(node),
        str(pnpm),
        "install",
        "--lockfile-only",
        "--ignore-scripts",
        "--strict-peer-dependencies",
        "--store-dir",
        str(SCRATCH / "store"),
        # The manifest pins packageManager to this exact pnpm; do not let pnpm
        # decide it should fetch one anyway.
        "--config.manage-package-manager-versions=false",
    ]
    completed = subprocess.run(
        argv, cwd=workspace, env=environment, check=False, text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise SystemExit(
            f"pnpm exited {completed.returncode}\n{completed.stdout}\n{completed.stderr}"
        )
    return (workspace / "pnpm-lock.yaml").read_text("utf-8")


def validate_lock(lock: str) -> None:
    for forbidden in ('"""', "\\"):
        if forbidden in lock:
            raise SystemExit(
                f"resolved lock contains {forbidden!r}, which the module's "
                "plain string literal cannot carry"
            )
    try:
        document = yaml.safe_load(lock)
    except yaml.YAMLError as exc:
        raise SystemExit(f"resolved lock is not valid YAML: {exc}") from exc
    if not isinstance(document, dict) or document.get("lockfileVersion") != "9.0":
        raise SystemExit("resolved lock is not a pnpm lockfile version 9.0")
    importers = set(document.get("importers") or {})
    if importers != nextjs._LOCK_IMPORTERS:
        raise SystemExit(
            "resolved lock importers differ from the pack's largest scaffold: "
            f"{sorted(importers ^ nextjs._LOCK_IMPORTERS)}"
        )
    if nextjs._LOCK_SCOPE_PLACEHOLDER not in lock:
        raise SystemExit("resolved lock carries no package-scope placeholder")


def rewritten_module(module: str, lock: str) -> str:
    """The lock module with a new template and everything before it kept."""

    head, marker, _ = module.partition(TEMPLATE_MARKER)
    if not marker:
        raise SystemExit(f"{LOCK_MODULE} has no {TEMPLATE_MARKER!r}")
    return f'{head}{marker}{lock}"""\n'


def current_template() -> str:
    return nextjs.PNPM_LOCK_TEMPLATE


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="resolve the lock and exit 1 if the module would change",
    )
    parser.add_argument(
        "--node-root",
        help="root of a Node 22.22.3 distribution when it is not first on PATH",
    )
    arguments = parser.parse_args(argv)

    lock = resolve_lock(arguments.node_root)
    validate_lock(lock)
    before = current_template()
    changed = [
        line
        for line in difflib.unified_diff(
            before.splitlines(), lock.splitlines(), lineterm="", n=0
        )
        if line[:1] in "+-" and line[:3] not in {"+++", "---"}
    ]
    if before == lock:
        print(f"{LOCK_MODULE.relative_to(ROOT)}: already current")
        return 0
    if arguments.check:
        print(
            f"{LOCK_MODULE.relative_to(ROOT)}: stale, "
            f"{len(changed)} lines would change"
        )
        return 1
    LOCK_MODULE.write_text(
        rewritten_module(LOCK_MODULE.read_text("utf-8"), lock), "utf-8"
    )
    print(f"{LOCK_MODULE.relative_to(ROOT)}: rewritten, {len(changed)} lines changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
