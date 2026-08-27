import json
from pathlib import Path
import threading
import time

import pytest

from richbuild.executor import (
    BubblewrapExecutor,
    SandboxPolicy,
    SandboxPolicyError,
    SandboxUnavailable,
    ToolAlias,
    ToolMount,
    TrustedNodePnpmRuntime,
    WorkspaceBootstrapError,
    WorkspaceBootstrapper,
)


class _DirectProcessExecutor(BubblewrapExecutor):
    """Exercise process lifecycle code without requiring nested namespaces."""

    def command(self, workspace, argv, policy):
        assert Path(workspace).is_dir()
        policy.validate()
        return list(argv)


def _term_ignoring_tree_script(
    *,
    started: Path,
    child_pid: Path,
    late_marker: Path,
) -> str:
    child = "\n".join(
        (
            "import os",
            "import signal",
            "import time",
            "from pathlib import Path",
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)",
            f"Path({str(child_pid)!r}).write_text(str(os.getpid()))",
            "time.sleep(0.6)",
            f"Path({str(late_marker)!r}).write_text('late')",
        )
    )
    return "\n".join(
        (
            "import signal",
            "import subprocess",
            "import sys",
            "import time",
            "from pathlib import Path",
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)",
            f"subprocess.Popen([sys.executable, '-c', {child!r}])",
            f"child = Path({str(child_pid)!r})",
            "deadline = time.monotonic() + 2",
            "while not child.exists() and time.monotonic() < deadline:",
            "    time.sleep(0.01)",
            f"Path({str(started)!r}).write_text('started')",
            "time.sleep(10)",
        )
    )


def _assert_process_tree_is_dead(child_pid: Path) -> None:
    """Assert the reaped grandchild is no longer running.

    The executor's contract is that no *live* member of the process group
    survives; a SIGKILLed grandchild is reparented, so its ``/proc`` entry can
    linger briefly as a zombie before init collects it. Poll for the entry to
    disappear, and accept the zombie state as dead — asserting on the raw
    ``/proc`` entry alone makes this test fail under load.
    """

    entry = Path("/proc", child_pid.read_text())
    deadline = time.monotonic() + 2
    while entry.exists() and time.monotonic() < deadline:
        if _process_state(entry) == "Z":
            return
        time.sleep(0.01)
    assert not entry.exists() or _process_state(entry) == "Z"


def _process_state(entry: Path) -> str | None:
    try:
        stat_line = (entry / "stat").read_text(errors="replace")
    except OSError:
        return None
    return stat_line[stat_line.rfind(")") + 2 :].split()[0]


def test_command_denies_network_and_mounts_only_approved_writes(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "out").mkdir()
    executor = BubblewrapExecutor(executable="/usr/bin/bwrap")

    command = executor.command(
        tmp_path,
        ["/usr/bin/python3", "-c", "print('ok')"],
        SandboxPolicy(writable_paths=("out",), working_directory="src"),
    )

    assert "--unshare-net" in command
    assert any(
        command[index : index + 3] == ["--ro-bind", str(tmp_path), "/workspace"]
        for index in range(len(command) - 2)
    )
    bind_index = command.index("--bind")
    assert command[bind_index + 1 : bind_index + 3] == [
        str(tmp_path / "out"),
        "/workspace/out",
    ]
    assert command[-3:] == ["/usr/bin/python3", "-c", "print('ok')"]


@pytest.mark.parametrize("path", ["/etc", "../escape", "src/../../escape"])
def test_workspace_paths_cannot_escape(path, tmp_path):
    executor = BubblewrapExecutor(executable="/usr/bin/bwrap")

    with pytest.raises(SandboxPolicyError, match="inside the workspace"):
        executor.command(
            tmp_path,
            ["/usr/bin/true"],
            SandboxPolicy(writable_paths=(path,)),
        )


def test_executor_fails_closed_without_bubblewrap(tmp_path):
    executor = BubblewrapExecutor(executable="/definitely/missing/bwrap")

    with pytest.raises(SandboxUnavailable, match="required"):
        executor.run(tmp_path, ["/usr/bin/true"])


def test_unsafe_loader_environment_is_rejected():
    with pytest.raises(SandboxPolicyError, match="unsafe environment"):
        SandboxPolicy(environment={"LD_PRELOAD": "/workspace/attack.so"}).validate()


def test_environment_values_never_appear_in_bubblewrap_arguments(tmp_path):
    executor = BubblewrapExecutor(executable="/usr/bin/bwrap")
    secret = "postgresql://owner:top-secret@database.invalid/app"

    command = executor.command(
        tmp_path,
        ["/usr/bin/env"],
        SandboxPolicy(network=True, environment={"DATABASE_URL": secret}),
    )

    assert secret not in command
    assert "DATABASE_URL" not in command
    assert "--clearenv" not in command
    assert "--unshare-net" not in command
    assert ["/etc/resolv.conf", "/etc/resolv.conf"] == command[
        command.index("/etc/resolv.conf") : command.index("/etc/resolv.conf") + 2
    ]


def test_trusted_tool_bundle_is_read_only_and_added_to_runtime_path(tmp_path):
    tool_root = tmp_path / "node"
    (tool_root / "bin").mkdir(parents=True)
    (tool_root / "bin" / "node").write_text("trusted")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executor = BubblewrapExecutor(
        executable="/usr/bin/bwrap",
        tool_mounts=(
            ToolMount(tool_root, "/opt/rich-tools/node"),
        ),
    )

    command = executor.command(
        workspace,
        ["/opt/rich-tools/node/bin/node", "--version"],
        SandboxPolicy(),
    )

    mount = command.index(str(tool_root.resolve()))
    assert command[mount - 1 : mount + 2] == [
        "--ro-bind",
        str(tool_root.resolve()),
        "/opt/rich-tools/node",
    ]


def test_existing_file_can_be_the_exact_writable_mount(tmp_path):
    generated = tmp_path / "next-env.d.ts"
    generated.write_text("generated")
    executor = BubblewrapExecutor(executable="/usr/bin/bwrap")

    command = executor.command(
        tmp_path,
        ["/usr/bin/true"],
        SandboxPolicy(writable_paths=("next-env.d.ts",)),
    )

    bind = command.index("--bind")
    assert command[bind + 1 : bind + 3] == [
        str(generated.resolve()),
        "/workspace/next-env.d.ts",
    ]
    assert generated.is_file()


def test_trusted_tool_alias_is_created_inside_the_sandbox(tmp_path):
    tool_root = tmp_path / "pnpm"
    (tool_root / "bin").mkdir(parents=True)
    (tool_root / "bin" / "pnpm.cjs").write_text("trusted")
    executor = BubblewrapExecutor(
        executable="/usr/bin/bwrap",
        tool_mounts=(ToolMount(tool_root, "/opt/rich-tools/pnpm"),),
        tool_aliases=(
            ToolAlias(
                "/opt/rich-tools/pnpm/bin/pnpm.cjs",
                "/opt/rich-tools/bin/pnpm",
            ),
        ),
    )

    command = executor.command(
        tmp_path,
        ["/opt/rich-tools/bin/pnpm", "--version"],
        SandboxPolicy(),
    )

    alias = command.index("--symlink")
    assert command[alias + 1 : alias + 3] == [
        "/opt/rich-tools/pnpm/bin/pnpm.cjs",
        "/opt/rich-tools/bin/pnpm",
    ]


@pytest.mark.parametrize(
    ("target", "guest"),
    [
        ("/usr/bin/false", "/opt/rich-tools/bin/pnpm"),
        ("/opt/rich-tools/pnpm/bin/pnpm.cjs", "/usr/bin/pnpm"),
        ("/opt/rich-tools/pnpm/bin/pnpm.cjs", "/opt/rich-tools/bin/sub/pnpm"),
    ],
)
def test_trusted_tool_aliases_fail_closed(target, guest, tmp_path):
    executor = BubblewrapExecutor(
        executable="/usr/bin/bwrap",
        tool_aliases=(ToolAlias(target, guest),),
    )

    with pytest.raises(SandboxPolicyError, match="tool alias"):
        executor.command(tmp_path, ["/usr/bin/true"], SandboxPolicy())


def test_writable_path_cannot_cross_existing_symlink(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (tmp_path / "escape").symlink_to(outside, target_is_directory=True)
    executor = BubblewrapExecutor(executable="/usr/bin/bwrap")

    with pytest.raises(SandboxPolicyError, match="symbolic link"):
        executor.command(
            tmp_path,
            ["/usr/bin/true"],
            SandboxPolicy(writable_paths=("escape/results",)),
        )


def test_deadline_kills_term_ignoring_process_tree_and_reaps_it(tmp_path):
    started = tmp_path / "started"
    child_pid = tmp_path / "child.pid"
    late_marker = tmp_path / "late"
    executor = _DirectProcessExecutor(
        executable="/usr/bin/python3",
        poll_interval_seconds=0.01,
        termination_grace_seconds=0.05,
    )
    script = _term_ignoring_tree_script(
        started=started,
        child_pid=child_pid,
        late_marker=late_marker,
    )

    result = executor.run(
        tmp_path,
        ("/usr/bin/python3", "-c", script),
        SandboxPolicy(timeout_seconds=5),
        deadline=time.monotonic() + 0.2,
    )

    assert result.timed_out
    assert not result.cancelled
    assert result.duration_seconds < 1
    assert started.is_file()
    assert executor.wait_for_idle(0.1)
    _assert_process_tree_is_dead(child_pid)
    time.sleep(0.65)
    assert not late_marker.exists()


def test_workspace_bootstrap_cancellation_reaps_process_tree(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "apps" / "web").mkdir(parents=True)
    (workspace / "package.json").write_text(
        json.dumps({"packageManager": "pnpm@10.34.5"})
    )
    (workspace / "apps" / "web" / "package.json").write_text(
        json.dumps({"name": "@app/web"})
    )
    (workspace / "pnpm-lock.yaml").write_text(
        "lockfileVersion: '9.0'\n"
        "importers:\n"
        "  .: {}\n"
        "  apps/web: {}\n"
    )
    started = tmp_path / "bootstrap.started"
    child_pid = tmp_path / "bootstrap-child.pid"
    late_marker = tmp_path / "bootstrap-late"
    script = _term_ignoring_tree_script(
        started=started,
        child_pid=child_pid,
        late_marker=late_marker,
    )

    class BlockingBootstrapExecutor(_DirectProcessExecutor):
        def command(self, received_workspace, argv, policy):
            assert tuple(argv)
            return super().command(
                received_workspace,
                ("/usr/bin/python3", "-c", script),
                policy,
            )

    executor = BlockingBootstrapExecutor(
        executable="/usr/bin/python3",
        poll_interval_seconds=0.01,
        termination_grace_seconds=0.05,
    )
    runtime = TrustedNodePnpmRuntime(
        executor=executor,
        node_executable="/opt/rich-tools/node/bin/node",
        pnpm_script="/opt/rich-tools/pnpm/bin/pnpm.cjs",
    )
    bootstrapper = WorkspaceBootstrapper(runtime, timeout_seconds=5)
    cancellation = threading.Event()
    outcome = {}

    def bootstrap() -> None:
        try:
            bootstrapper.bootstrap(
                workspace,
                cancellation=cancellation.is_set,
            )
        except Exception as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=bootstrap)
    thread.start()
    deadline = time.monotonic() + 2
    while not started.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert started.is_file()
    cancellation.set()
    thread.join(2)

    assert not thread.is_alive()
    assert isinstance(outcome.get("error"), WorkspaceBootstrapError)
    assert "canceled" in str(outcome["error"])
    assert bootstrapper.wait_for_idle(0.1)
    _assert_process_tree_is_dead(child_pid)
    time.sleep(0.65)
    assert not late_marker.exists()


@pytest.mark.live
def test_bubblewrap_conformance_read_write_and_isolation(tmp_path):
    """Opt-in host conformance; nested CI sandboxes may disable user namespaces."""
    (tmp_path / "read.txt").write_text("visible")
    (tmp_path / "out").mkdir()
    executor = BubblewrapExecutor()
    if not executor.available():
        pytest.skip("Bubblewrap is not installed")

    script = (
        "from pathlib import Path; "
        "assert Path('read.txt').read_text() == 'visible'; "
        "Path('out/result.txt').write_text('written'); "
        "assert not Path('/home').exists()"
    )
    result = executor.run(
        tmp_path,
        ["/usr/bin/python3", "-c", script],
        SandboxPolicy(writable_paths=("out",)),
    )
    if "Operation not permitted" in result.stderr:
        pytest.skip("host does not permit nested user/network namespaces")

    assert result.passed, result.stderr
    assert (tmp_path / "out" / "result.txt").read_text() == "written"
    assert not (tmp_path / "result.txt").exists()
