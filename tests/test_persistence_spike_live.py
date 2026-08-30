"""The M7 spike, kept as the model-free proof of the pack's persistence.

Design A in docs/program.md puts PGlite -- Postgres compiled to WebAssembly,
in-process, socket-free, filesystem-backed -- inside the network-off Bubblewrap
gates, and real Postgres on Neon at preview. The spike asked whether that
engine could start under the sandbox as it stands: ``--unshare-net``, a 16 GiB
address-space ceiling for Node gates with the WebAssembly trap handler
disabled, a 1.5 GiB V8 heap, bounded processes. It could, twice.

The pack now renders what the spike carried as fixtures -- the engine-selecting
factory, the migrator, the probe -- so this test runs those protected files
exactly as the engine will, with only what a worker would author laid over the
scaffold: a schema, a migration, a domain module, a page. In one run, on a
scaffold the pack renders:

a. lint, typecheck and unit gates over the rendered tree -- the typecheck is
   the first real ``tsc`` the protected TypeScript faces;
b. the trusted prepare step, ``pnpm -C packages/db exec tsx src/migrate.ts``,
   under the Node-gate policy with ``RICH_DATABASE_DIR`` set, which prints the
   ``RICH_DATABASE_MIGRATIONS`` line the engine records; then the read-only
   probe over the same directory;
c. ``next build --webpack`` with no database in the environment, checking that
   both drivers stay externals and no server chunk bundles the engine;
d. a fresh directory, migrated again, then ``next start`` under Playwright
   running the approved ``fill -> click -> reload -> assert_visible`` oracle,
   then the probe, which must count the row the browser created.

It measures wall time and the peak virtual size of every process the sandbox
ran (RLIMIT_AS is per process, so the largest single ``VmPeak`` is the number
that matters). Every command goes through ``BubblewrapCommandRunner`` exactly
as the engine sends it: the gates as ``VerificationCommand``s, the migrator
and the probe as ``DatabaseStep``s, so the policy each one sees -- the
database directory writable and ``RICH_DATABASE_DIR`` set for the kinds that
run the software, and for no other -- is the runner's own, not a transcript.

It is skipped by default: it downloads the locked dependency graph and
Chromium (about 2 GiB) into the workspace, or into ``RICH_LIVE_CACHE_ROOT``
when that is set. No model is called.

    RICH_LIVE_CACHE_ROOT=.rich/live-cache python -m pytest --run-live \\
        --basetemp=.rich/live-m7 -q -s tests/test_persistence_spike_live.py
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import threading

import pytest

from liveutil import live_cache_root

from richbuild.executor import (
    ExecutionResult,
    SandboxUnavailable,
    WorkspaceBootstrapper,
    trusted_node_pnpm_runtime,
)
from richbuild.interview import AdaptiveInterview, InterviewState
from richbuild.models import NodeKind
from richbuild.planner import plan_nextjs_architecture
from richbuild.run_engine import (
    DATABASE_PREPARE,
    DATABASE_PROBE,
    AcceptanceCoverageContext,
    BubblewrapCommandRunner,
    DatabaseStep,
    VerificationCommand,
    _observed_acceptance_coverage,
)
from richbuild.runtime import PinnedRunCommands
from richbuild.target_packs.nextjs import (
    DATABASE_DIRECTORY,
    DATABASE_PROBE_PATH,
    NextJsTargetPack,
    NextJsTargetPackConfig,
    _route_segments,
)

FIXTURES = Path(__file__).parent / "fixtures" / "persistence_spike"
MIGRATIONS_PREFIX = "RICH_DATABASE_MIGRATIONS "
PROBE_PREFIX = "RICH_DATABASE_PROBE "
GATE_TIMEOUT_SECONDS = 900.0
TODO_REQUIREMENT = "req.todo"
SCENARIO = "scenario.persist"


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

    Only what a worker would write into owned paths. The factory, the
    migrator and the probe are the pack's own protected files now, and this
    test exists to run those, not copies of them.
    """

    route = f"/capabilities/{_route_segments((TODO_REQUIREMENT,))[TODO_REQUIREMENT]}"
    for source, destination in {
        "schema.ts": "packages/db/src/schema.ts",
        "0000_initial.sql": "packages/db/migrations/0000_initial.sql",
    }.items():
        shutil.copyfile(FIXTURES / source, workspace / destination)
    (workspace / "packages/domain/src/todos.ts").write_text(
        (FIXTURES / "todos.ts").read_text("utf-8").replace("__SCOPE__", scope), "utf-8"
    )
    page = workspace / f"apps/web/src/app{route}/page.tsx"
    assert 'export const dynamic = "force-dynamic";' in page.read_text("utf-8"), (
        "the pack renders a persisting capability's page dynamic"
    )
    page.write_text(
        (FIXTURES / "page.tsx")
        .read_text("utf-8")
        .replace("__SCOPE__", scope)
        .replace("__ROUTE__", route),
        "utf-8",
    )
    index = workspace / "packages/domain/src/index.ts"
    index.write_text(index.read_text("utf-8") + '\nexport * from "./todos";\n', "utf-8")
    config = (workspace / "apps/web/next.config.mjs").read_text("utf-8")
    assert (
        'serverExternalPackages: ["@electric-sql/pglite", "postgres"]' in config
    ), "the pack renders the externals itself"
    for protected in ("packages/db/src/database.ts", "packages/db/src/migrate.ts"):
        assert (workspace / protected).is_file(), protected
    assert (workspace / DATABASE_PROBE_PATH).is_file()
    return route


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


def _report(result: ExecutionResult, prefix: str) -> dict:
    assert result.passed, f"{prefix.strip()} step failed:\n{result.stdout}\n{result.stderr}"
    lines = [
        line[len(prefix) :]
        for line in result.stdout.splitlines()
        if line.startswith(prefix)
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
    cache = live_cache_root()
    bootstrapper = WorkspaceBootstrapper(toolchain, cache_root=cache)
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
    migration = workspace / "packages/db/migrations/0000_initial.sql"
    expected_digest = hashlib.sha256(migration.read_bytes()).hexdigest()

    prepared = bootstrapper.bootstrap(workspace)
    assert prepared.passed, (
        f"bootstrap failed:\n{prepared.dependency_install.stdout}\n"
        f"{prepared.dependency_install.stderr}"
    )
    measurements: dict[str, object] = {
        "route": route,
        "cache_root": str(cache) if cache else None,
        "bootstrap": {
            "seconds": round(
                prepared.dependency_install.duration_seconds
                + (prepared.browser_install.duration_seconds if prepared.browser_install else 0),
                1,
            )
        },
    }
    runner = BubblewrapCommandRunner(
        executor, timeout_seconds=GATE_TIMEOUT_SECONDS, cache_root=cache
    )
    assert runner.database_directory == DATABASE_DIRECTORY
    assert commands.probe_argv[-1] == DATABASE_PROBE_PATH
    database = workspace / DATABASE_DIRECTORY
    prepare = DatabaseStep(DATABASE_PREPARE, commands.database_argv)
    probe = DatabaseStep(DATABASE_PROBE, commands.probe_argv)

    # (a) The gates the protected files must pass, exactly as the engine runs
    # them. The typecheck is the first real tsc the factory and migrator face.
    for kind, argv in (
        ("lint", commands.lint_argv),
        ("static", commands.static_argv),
        ("unit", commands.unit_argv),
    ):
        result = runner.run(workspace, VerificationCommand(kind, argv))
        assert result.passed, f"{kind} gate:\n{result.stdout}\n{result.stderr}"
        measurements[kind] = _measured(result, None)

    # (b) The trusted prepare step under the Node gate's own ceiling, then the
    # read-only probe over the directory it left behind.
    shutil.rmtree(database, ignore_errors=True)
    with AddressSpaceWatch() as watch:
        result = runner.run(workspace, prepare)
    migrated = _report(result, MIGRATIONS_PREFIX)
    measurements["migrate"] = {
        **_measured(result, watch),
        "engine": migrated["engine"],
        "migrations": migrated["migrations"],
    }
    assert migrated["schema_version"] == "rich.database-migrations/v1"
    assert migrated["engine"]["name"] == "pglite"
    assert "PostgreSQL" in migrated["engine"]["server_version"], migrated
    assert migrated["migrations"] == [
        {"file": "0000_initial.sql", "sha256": expected_digest, "applied": True}
    ], migrated
    # Again over the same directory: nothing to apply, same journal.
    result = runner.run(workspace, prepare)
    assert _report(result, MIGRATIONS_PREFIX)["migrations"] == [
        {"file": "0000_initial.sql", "sha256": expected_digest, "applied": False}
    ]
    result = runner.run(workspace, probe)
    fresh = _report(result, PROBE_PREFIX)
    measurements["probe"] = {**_measured(result, None), "engine": fresh["engine"]}
    assert fresh["schema_version"] == "rich.database-probe/v1"
    assert fresh["tables"] == {"projects": 0, "todos": 0}, fresh
    assert fresh["migrations"] == [{"file": "0000_initial.sql", "sha256": expected_digest}]

    # (c) The production build, with no database in its environment.
    with AddressSpaceWatch() as watch:
        result = runner.run(workspace, VerificationCommand("build", commands.build_argv))
    assert result.passed, f"build gate:\n{result.stdout}\n{result.stderr}"
    externals = external_requires(workspace)
    measurements["build"] = {**_measured(result, watch), "externals": externals}
    assert any("pglite" in request for request in externals["externals"]), externals
    assert not externals["chunks_bundling_pglite"], externals

    # (d) A fresh directory, migrated by the trusted step, then the oracle
    # bound to an attempt the way the engine binds it, then the probe over
    # what the browser left behind.
    shutil.rmtree(database)
    _report(runner.run(workspace, prepare), MIGRATIONS_PREFIX)
    context = AcceptanceCoverageContext(
        run_id="run.spike", task_id="task.spike", attempt=1, nonce="ab" * 32
    )
    with AddressSpaceWatch() as watch:
        result = runner.run(
            workspace,
            VerificationCommand(
                "acceptance",
                commands.acceptance_argv,
                expected_acceptance_scenario_ids=tuple(sorted(project.scenario_index)),
                acceptance_context=context,
            ),
        )
    measurements["acceptance"] = _measured(result, watch)
    assert result.passed, f"acceptance gate:\n{result.stdout}\n{result.stderr}"
    coverage = _observed_acceptance_coverage(
        result,
        expected_scenario_ids=tuple(sorted(project.scenario_index)),
        expected_context=context,
    )
    assert SCENARIO in coverage, coverage
    result = runner.run(workspace, probe)
    after = _report(result, PROBE_PREFIX)
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
    assert after["migrations"] == [{"file": "0000_initial.sql", "sha256": expected_digest}]
    limit_kb = runner.max_memory_bytes // 1024
    assert measurements["migrate"]["peak_address_space"]["kb"] < limit_kb, summary
    assert measurements["build"]["peak_address_space"]["kb"] < limit_kb, summary
