"""The M7 spike: can the database live inside the gates?

Design A in docs/program.md puts PGlite -- Postgres compiled to WebAssembly,
in-process, socket-free, filesystem-backed -- inside the network-off Bubblewrap
gates, and real Postgres on Neon at preview. Nothing else in the milestone is
worth building if that engine cannot start under the sandbox as it stands:
``--unshare-net``, a 16 GiB address-space ceiling for Node gates with the
WebAssembly trap handler disabled, a 1.5 GiB V8 heap, bounded processes. This
test asks exactly that question, in one run, on a scaffold the pack renders:

a. a trusted probe script opens ``new PGlite(dir)`` on a writable path, applies
   ``packages/db/migrations/*.sql`` with the algorithm ``preview.py`` uses on
   Neon, inserts a row, reads it back, and prints ``RICH_DATABASE_PROBE``;
b. ``next build --webpack`` with ``serverExternalPackages`` for the two drivers
   and a fixture-authored server-action page that writes through
   ``packages/domain`` -> ``packages/db``, with no database in the environment;
c. ``next start`` under Playwright running the approved
   ``fill -> click -> reload -> assert_visible`` oracle with
   ``RICH_DATABASE_DIR`` set, then the probe again over the same directory.

It measures wall time and the peak virtual size of every process the sandbox
ran (RLIMIT_AS is per process, so the largest single ``VmPeak`` is the number
that matters), and records how the external requires were emitted. The two
deltas from today's gate policy are exactly the ones step 4 of the milestone
introduces -- ``.rich/runtime/db`` writable and ``RICH_DATABASE_DIR`` in the
environment -- and nothing else is loosened.

It is skipped by default: it downloads the locked dependency graph and
Chromium (about 2 GiB) into the workspace. No model is called.

    python -m pytest --run-live --basetemp=.rich/live-m7 -q -s \\
        tests/test_persistence_spike_live.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import threading

import pytest

from richbuild.executor import (
    ExecutionResult,
    SandboxPolicy,
    SandboxUnavailable,
    WorkspaceBootstrapper,
    cache_mounts_for,
    trusted_node_pnpm_runtime,
)
from richbuild.interview import AdaptiveInterview, InterviewState
from richbuild.models import NodeKind
from richbuild.planner import plan_nextjs_architecture
from richbuild.run_engine import (
    AcceptanceCoverageContext,
    BubblewrapCommandRunner,
    VerificationCommand,
    _observed_acceptance_coverage,
)
from richbuild.runtime import PinnedRunCommands
from richbuild.target_packs.nextjs import (
    NextJsTargetPack,
    NextJsTargetPackConfig,
    _route_segments,
)

FIXTURES = Path(__file__).parent / "fixtures" / "persistence_spike"
DATABASE_DIRECTORY = ".rich/runtime/db"
PROBE = ".rich/verify-database.mjs"
PROBE_PREFIX = "RICH_DATABASE_PROBE "
GATE_TIMEOUT_SECONDS = 900.0
TODO_REQUIREMENT = "req.todo"
SCENARIO = "scenario.persist"
# Mirrors BubblewrapCommandRunner.run, which has no seam for a gate-specific
# variable today; step 4 gives it one for RICH_DATABASE_DIR.
GATE_ENVIRONMENT = {
    "CI": "1",
    "NEXT_TELEMETRY_DISABLED": "1",
    "NODE_OPTIONS": "--disable-wasm-trap-handler --max-old-space-size=1536",
    "RAYON_NUM_THREADS": "2",
}


def spike_project():
    """A todo list whose one scenario is the drive's 'reloads -- it is still there'."""

    return AdaptiveInterview(
        InterviewState(
            "project.persistence-spike",
            "Persistence Spike",
            answers={
                "goal": (
                    "Keep a list of items that survives a page reload; every item "
                    "is stored in the database."
                ),
                "audiences": ["members"],
                "data_policy": ["An item is kept until a member deletes it."],
                "capabilities": [
                    {
                        "id": TODO_REQUIREMENT,
                        "title": "Todo list",
                        "statement": (
                            "A member adds an item and it is still there after a reload."
                        ),
                    }
                ],
                "quality_constraints": [
                    {
                        "id": "req.a11y",
                        "title": "Keyboard access",
                        "statement": "The list is operable with a keyboard.",
                    }
                ],
                "scenarios": [
                    {
                        "id": SCENARIO,
                        "title": "An item persists across a reload",
                        "when": ["A member adds 'Buy milk'.", "They reload the page."],
                        "then": ["'Buy milk' is still listed."],
                        "requirement_ids": [TODO_REQUIREMENT],
                        "oracle": [
                            {"action": "open_requirement"},
                            {
                                "action": "fill",
                                "locator": {"kind": "label", "value": "New item"},
                                "value": "Buy milk",
                            },
                            {
                                "action": "click",
                                "locator": {
                                    "kind": "role",
                                    "value": "button",
                                    "name": "Add",
                                },
                            },
                            {"action": "reload"},
                            {
                                "action": "assert_visible",
                                "locator": {"kind": "text", "value": "Buy milk"},
                            },
                        ],
                    },
                    {
                        "id": "scenario.keyboard",
                        "title": "Keyboard navigation",
                        "when": ["A member uses only a keyboard."],
                        "then": ["The list remains operable."],
                        "requirement_ids": ["req.a11y"],
                        "oracle": [
                            {"action": "open_requirement"},
                            {
                                "action": "assert_visible",
                                "locator": {
                                    "kind": "text",
                                    "value": "The list is operable with a keyboard.",
                                },
                            },
                        ],
                    },
                ],
            },
        )
    ).compile()


def apply_spike_fixtures(workspace: Path, scope: str) -> str:
    """Author the spike's application over a fresh scaffold; return its route.

    Everything written here is what step 3 of the milestone will render as
    protected scaffold or what a worker would author into owned paths. The
    scaffold's postgres-only ``migrate.ts``/``seed.ts`` and drizzle's journal
    are removed rather than adapted: step 3 replaces them, and here they would
    only fail the typecheck.
    """

    route = f"/capabilities/{_route_segments((TODO_REQUIREMENT,))[TODO_REQUIREMENT]}"
    for source, destination in {
        "verify-database.mjs": PROBE,
        "database.ts": "packages/db/src/database.ts",
        "db-index.ts": "packages/db/src/index.ts",
        "schema.ts": "packages/db/src/schema.ts",
        "0000_initial.sql": "packages/db/migrations/0000_initial.sql",
    }.items():
        shutil.copyfile(FIXTURES / source, workspace / destination)
    (workspace / "packages/domain/src/todos.ts").write_text(
        (FIXTURES / "todos.ts").read_text("utf-8").replace("__SCOPE__", scope), "utf-8"
    )
    (workspace / f"apps/web/src/app{route}/page.tsx").write_text(
        (FIXTURES / "page.tsx")
        .read_text("utf-8")
        .replace("__SCOPE__", scope)
        .replace("__ROUTE__", route),
        "utf-8",
    )
    for stale in (
        "packages/db/src/migrate.ts",
        "packages/db/src/seed.ts",
        "packages/db/migrations/meta/_journal.json",
    ):
        (workspace / stale).unlink()
    (workspace / "packages/db/migrations/meta").rmdir()
    index = workspace / "packages/domain/src/index.ts"
    index.write_text(index.read_text("utf-8") + '\nexport * from "./todos";\n', "utf-8")
    config = workspace / "apps/web/next.config.mjs"
    marker = "  poweredByHeader: false,\n"
    text = config.read_text("utf-8")
    assert marker in text
    config.write_text(
        text.replace(
            marker,
            marker + '  serverExternalPackages: ["@electric-sql/pglite", "postgres"],\n',
            1,
        ),
        "utf-8",
    )
    return route


def gate_policy(
    runner: BubblewrapCommandRunner, *, acceptance: bool, database: bool
) -> SandboxPolicy:
    """Today's gate policy, plus the two deltas step 4 introduces when asked."""

    environment = {
        **GATE_ENVIRONMENT,
        "PLAYWRIGHT_BROWSERS_PATH": runner.playwright_browsers_path,
    }
    writable = runner.writable_paths
    if database:
        environment["RICH_DATABASE_DIR"] = f"/workspace/{DATABASE_DIRECTORY}"
        writable = (*writable, DATABASE_DIRECTORY)
    return SandboxPolicy(
        writable_paths=writable,
        network=False,
        environment=environment,
        cache_mounts=(
            cache_mounts_for(runner.cache_root, writable=False)
            if runner.cache_root is not None
            else ()
        ),
        timeout_seconds=runner.timeout_seconds,
        max_memory_bytes=(
            runner.acceptance_max_address_space_bytes
            if acceptance
            else runner.max_memory_bytes
        ),
        max_processes=runner.max_processes,
        max_cpu_seconds=max(1, int(runner.timeout_seconds)),
        max_output_bytes=runner.max_output_bytes,
    )


class AddressSpaceWatch:
    """Peak virtual size of every process the sandbox ran, by process name.

    RLIMIT_AS is enforced per process, so the number that matters is the largest
    single VmPeak, never a sum. Read from the host's procfs by ancestry: every
    process the executor launches descends from this one. A process shorter
    than one polling interval can be missed, which understates, never overstates.
    """

    def __init__(self) -> None:
        self.peak_kb: dict[str, int] = {}
        self.resident_kb: dict[str, int] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._poll, daemon=True)

    def __enter__(self) -> "AddressSpaceWatch":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join()

    @property
    def largest(self) -> tuple[str, int]:
        if not self.peak_kb:
            return ("", 0)
        name = max(self.peak_kb, key=self.peak_kb.__getitem__)
        return name, self.peak_kb[name]

    def _poll(self) -> None:
        me = os.getpid()
        while not self._stop.is_set():
            self._sample(me)
            self._stop.wait(0.25)
        self._sample(me)

    def _sample(self, me: int) -> None:
        parents: dict[int, int] = {}
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat = (entry / "stat").read_text()
            except OSError:
                continue
            fields = stat[stat.rfind(")") + 2 :].split()
            parents[int(entry.name)] = int(fields[1])
        for pid in parents:
            cursor, hops = pid, 0
            while cursor > 1 and hops < 64 and cursor != me:
                cursor, hops = parents.get(cursor, 0), hops + 1
            if cursor != me or pid == me:
                continue
            try:
                status = Path(f"/proc/{pid}/status").read_text()
            except OSError:
                continue
            name, peak, resident = "", 0, 0
            for line in status.splitlines():
                if line.startswith("Name:"):
                    name = line.split(None, 1)[1]
                elif line.startswith("VmPeak:"):
                    peak = int(line.split()[1])
                elif line.startswith("VmHWM:"):
                    resident = int(line.split()[1])
            self.peak_kb[name] = max(self.peak_kb.get(name, 0), peak)
            self.resident_kb[name] = max(self.resident_kb.get(name, 0), resident)


def probe_report(result: ExecutionResult) -> dict:
    assert result.passed, f"probe failed:\n{result.stdout}\n{result.stderr}"
    lines = [
        line[len(PROBE_PREFIX) :]
        for line in result.stdout.splitlines()
        if line.startswith(PROBE_PREFIX)
    ]
    assert len(lines) == 1, result.stdout
    return json.loads(lines[0])


def external_requires(workspace: Path) -> dict[str, object]:
    """How the built server chunks reach the drivers: external, or bundled.

    Both packages are ESM-first, so webpack emits them as module externals --
    ``import("@electric-sql/pglite")`` rather than ``require`` -- which Node
    resolves from the chunk's own location under apps/web/.next/server.
    """

    requests: set[str] = set()
    bundled: set[str] = set()
    for chunk in (workspace / "apps/web/.next/server").rglob("*.js"):
        text = chunk.read_text("utf-8", errors="replace")
        for kind, _quote, request in re.findall(
            r"""\b(require|import)\((["'])([^"']+)\2\)""", text
        ):
            if "pglite" in request or request.endswith("postgres"):
                requests.add(f"{kind}({request})")
        if "pglite.wasm" in text:
            bundled.add(str(chunk.relative_to(workspace)))
    return {"externals": sorted(requests), "chunks_bundling_pglite": sorted(bundled)}


def _measured(result: ExecutionResult, watch: AddressSpaceWatch | None) -> dict:
    record: dict[str, object] = {"seconds": round(result.duration_seconds, 1)}
    if watch is not None:
        name, peak = watch.largest
        record["peak_address_space"] = {
            "process": name,
            "kb": peak,
            "gib": round(peak / 1024 / 1024, 2),
        }
        record["peak_address_space_by_process_kb"] = dict(
            sorted(watch.peak_kb.items(), key=lambda item: -item[1])[:6]
        )
        record["peak_resident_by_process_kb"] = dict(
            sorted(watch.resident_kb.items(), key=lambda item: -item[1])[:6]
        )
    return record


@pytest.mark.live
def test_the_database_runs_inside_the_gates(tmp_path):
    if shutil.which("bwrap") is None:
        pytest.skip("live test; Bubblewrap is not on PATH")
    try:
        toolchain = trusted_node_pnpm_runtime()
    except SandboxUnavailable as exc:
        pytest.skip(f"live test; {exc}")
    bootstrapper = WorkspaceBootstrapper(toolchain)
    commands = PinnedRunCommands.for_toolchain(toolchain)
    executor = toolchain.executor

    project = spike_project()
    architecture = plan_nextjs_architecture(project).architecture
    assert any(node.kind is NodeKind.DATA for node in architecture.nodes)
    config = NextJsTargetPackConfig(
        project_name="persistence-spike",
        project_spec=project,
        architecture=architecture,
    )
    workspace = tmp_path / "workspace"
    NextJsTargetPack(config).scaffold(workspace)
    route = apply_spike_fixtures(workspace, config.scope)

    prepared = bootstrapper.bootstrap(workspace)
    assert prepared.passed
    measurements: dict[str, object] = {
        "route": route,
        "bootstrap": {
            "seconds": round(
                prepared.dependency_install.duration_seconds
                + (prepared.browser_install.duration_seconds if prepared.browser_install else 0),
                1,
            )
        },
    }
    runner = BubblewrapCommandRunner(executor, timeout_seconds=GATE_TIMEOUT_SECONDS)
    node = toolchain.node_executable
    database = workspace / DATABASE_DIRECTORY

    # The gates the fixture must not break, exactly as the engine runs them.
    for kind, argv in (
        ("lint", commands.lint_argv),
        ("static", commands.static_argv),
        ("unit", commands.unit_argv),
    ):
        result = runner.run(workspace, VerificationCommand(kind, argv))
        assert result.passed, f"{kind} gate:\n{result.stdout}\n{result.stderr}"
        measurements[kind] = _measured(result, None)

    # (a) The engine starts under the Node gate's own ceiling, migrates, writes, reads.
    shutil.rmtree(database, ignore_errors=True)
    with AddressSpaceWatch() as watch:
        result = executor.run(
            workspace,
            (node, PROBE, "--exercise"),
            gate_policy(runner, acceptance=False, database=True),
        )
    exercised = probe_report(result)
    measurements["probe_exercise"] = {
        **_measured(result, watch),
        "engine": exercised["engine"],
        "migrations": exercised["migrations"],
        "memory_inside": exercised["memory"],
        "duration_ms_inside": exercised["duration_ms"],
    }
    assert exercised["exercised"]["read_back"] is True, exercised
    assert exercised["tables"] == {"projects": 0, "todos": 1}, exercised
    assert [m["file"] for m in exercised["migrations"]] == ["0000_initial.sql"]
    assert exercised["migrations"][0]["applied"] is True

    # (b) The production build, with no database in its environment.
    with AddressSpaceWatch() as watch:
        result = runner.run(workspace, VerificationCommand("build", commands.build_argv))
    assert result.passed, f"build gate:\n{result.stdout}\n{result.stderr}"
    externals = external_requires(workspace)
    measurements["build"] = {**_measured(result, watch), "externals": externals}
    assert any("pglite" in request for request in externals["externals"]), externals
    assert not externals["chunks_bundling_pglite"], externals

    # (c) A fresh directory, migrated by the trusted step, then the oracle.
    shutil.rmtree(database)
    result = executor.run(
        workspace, (node, PROBE), gate_policy(runner, acceptance=False, database=True)
    )
    fresh = probe_report(result)
    assert fresh["tables"] == {"projects": 0, "todos": 0}, fresh
    measurements["probe_prepare"] = _measured(result, None)
    with AddressSpaceWatch() as watch:
        result = executor.run(
            workspace,
            commands.acceptance_argv,
            gate_policy(runner, acceptance=True, database=True),
        )
    measurements["acceptance"] = _measured(result, watch)
    assert result.passed, f"acceptance gate:\n{result.stdout}\n{result.stderr}"
    coverage = _observed_acceptance_coverage(
        result,
        expected_scenario_ids=tuple(sorted(project.scenario_index)),
        # No context file was handed in, so the config's standalone context applies.
        expected_context=AcceptanceCoverageContext(
            run_id="standalone", task_id="standalone", attempt=1, nonce="0" * 64
        ),
    )
    assert SCENARIO in coverage, coverage
    result = executor.run(
        workspace, (node, PROBE), gate_policy(runner, acceptance=False, database=True)
    )
    after = probe_report(result)
    measurements["probe_after_acceptance"] = {
        **_measured(result, None),
        "tables": after["tables"],
        "migrations": after["migrations"],
    }

    summary = json.dumps(measurements, indent=2)
    (tmp_path / "persistence-spike-measurements.json").write_text(summary, "utf-8")
    print(f"\nPERSISTENCE SPIKE MEASUREMENTS\n{summary}")
    assert after["tables"] == {"projects": 0, "todos": 1}, (
        "the row Playwright created is not in the directory the gate designated\n"
        + summary
    )
    assert after["migrations"][0]["applied"] is False, summary
    limit_kb = runner.max_memory_bytes // 1024
    assert measurements["probe_exercise"]["peak_address_space"]["kb"] < limit_kb, summary
    assert measurements["build"]["peak_address_space"]["kb"] < limit_kb, summary
