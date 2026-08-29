"""Fail-closed Bubblewrap executor for generated and agent-authored commands."""

from __future__ import annotations

from dataclasses import dataclass, field
import contextlib
import fcntl
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import resource
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from typing import Iterator, Callable, Mapping, Sequence

import yaml


TRUSTED_NODE_VERSION = "22.23.2"
TRUSTED_PNPM_VERSION = "10.34.5"
_NODE_VERSION_DEFINE = re.compile(
    r"^#define NODE_(MAJOR|MINOR|PATCH)_VERSION ([0-9]+)$",
    re.MULTILINE,
)
_MAX_MANIFEST_BYTES = 1024 * 1024
_MAX_LOCKFILE_BYTES = 4 * 1024 * 1024


class SandboxError(RuntimeError):
    """Base class for sandbox policy and execution errors."""


class SandboxUnavailable(SandboxError):
    """The host cannot provide the required isolation."""


class SandboxPolicyError(SandboxError):
    """A requested sandbox capability is outside the approved policy."""


class WorkspaceBootstrapError(SandboxError):
    """A generated workspace could not be bootstrapped under the trusted policy."""


@dataclass(frozen=True, slots=True)
class SandboxPolicy:
    """Capabilities approved for one command.

    Paths are relative to the declared workspace. The entire workspace is readable;
    only ``writable_paths`` are rebound writable. Network access is denied by default.
    """

    writable_paths: tuple[str, ...] = ()
    working_directory: str = "."
    network: bool = False
    environment: Mapping[str, str] = field(default_factory=dict)
    # Shared caches below /opt/rich-cache; see CacheMount for the trust rule.
    cache_mounts: tuple[CacheMount, ...] = ()
    timeout_seconds: float = 60
    max_memory_bytes: int = 1_073_741_824
    max_file_bytes: int = 268_435_456
    max_processes: int = 64
    max_cpu_seconds: int = 60
    max_output_bytes: int = 1_048_576

    def validate(self) -> None:
        if self.timeout_seconds <= 0:
            raise SandboxPolicyError("timeout must be positive")
        if min(
            self.max_memory_bytes,
            self.max_file_bytes,
            self.max_processes,
            self.max_cpu_seconds,
            self.max_output_bytes,
        ) <= 0:
            raise SandboxPolicyError("resource limits must be positive")
        _safe_relative(self.working_directory)
        for path in self.writable_paths:
            _safe_relative(path)
        guests = [PurePosixPath(mount.guest_path) for mount in self.cache_mounts]
        if len(set(guests)) != len(guests):
            raise SandboxPolicyError("cache mounts must target distinct guest paths")
        for guest in guests:
            if not guest.is_absolute() or CACHE_GUEST_ROOT not in guest.parents:
                raise SandboxPolicyError(
                    f"cache mounts must target a child of {CACHE_GUEST_ROOT}"
                )
        if any(
            not isinstance(key, str)
            or not isinstance(value, str)
            or not key
            or "=" in key
            or "\x00" in key
            or "\x00" in value
            for key, value in self.environment.items()
        ):
            raise SandboxPolicyError("environment must contain valid string entries")
        forbidden_environment = {
            key for key in self.environment if key in {"LD_PRELOAD", "LD_LIBRARY_PATH"}
        }
        if forbidden_environment:
            raise SandboxPolicyError(
                f"unsafe environment overrides: {sorted(forbidden_environment)}"
            )


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False
    cancelled: bool = False

    @property
    def passed(self) -> bool:
        return not self.timed_out and not self.cancelled and self.returncode == 0


@dataclass(frozen=True, slots=True)
class ToolMount:
    """A trusted, read-only host tool bundle mounted below /opt/rich-tools."""

    host_path: Path
    guest_path: str

    def validated(self) -> tuple[Path, PurePosixPath]:
        host = Path(self.host_path).resolve(strict=True)
        if not host.is_dir():
            raise SandboxPolicyError(f"tool mount is not a directory: {host}")
        guest = PurePosixPath(self.guest_path)
        trusted_root = PurePosixPath("/opt/rich-tools")
        if (
            not guest.is_absolute()
            or guest == trusted_root
            or trusted_root not in guest.parents
            or ".." in guest.parts
        ):
            raise SandboxPolicyError(
                "tool mounts must target a child of /opt/rich-tools"
            )
        return host, guest


CACHE_GUEST_ROOT = PurePosixPath("/opt/rich-cache")
CACHE_STORE_GUEST = str(CACHE_GUEST_ROOT / "pnpm-store")
CACHE_BROWSERS_GUEST = str(CACHE_GUEST_ROOT / "playwright")


@dataclass(frozen=True, slots=True)
class CacheMount:
    """A shared dependency cache mounted below /opt/rich-cache.

    Writable only during the trusted bootstrap -- pnpm and Playwright's own
    installers, lifecycle scripts disabled, no generated code running -- and
    read-only for every gate, so a run's own source can never alter the
    store a later run installs from or the browser a later run is judged by.
    """

    host_path: Path
    guest_path: str
    writable: bool = False

    def validated(self) -> tuple[Path, PurePosixPath]:
        host = Path(self.host_path).resolve(strict=True)
        if not host.is_dir():
            raise SandboxPolicyError(f"cache mount is not a directory: {host}")
        guest = PurePosixPath(self.guest_path)
        if (
            not guest.is_absolute()
            or guest == CACHE_GUEST_ROOT
            or CACHE_GUEST_ROOT not in guest.parents
            or ".." in guest.parts
        ):
            raise SandboxPolicyError(
                f"cache mounts must target a child of {CACHE_GUEST_ROOT}"
            )
        return host, guest


def cache_mounts_for(cache_root: str | Path, *, writable: bool) -> tuple[CacheMount, ...]:
    """The two mounts a shared cache root provides: the pnpm store and the browsers.

    Created here so a first run finds them; a later run finds them full.
    """

    root = Path(cache_root).resolve()
    store = root / "pnpm-store"
    browsers = root / "playwright"
    for directory in (store, browsers):
        directory.mkdir(parents=True, exist_ok=True)
    return (
        CacheMount(store, CACHE_STORE_GUEST, writable),
        CacheMount(browsers, CACHE_BROWSERS_GUEST, writable),
    )


@dataclass(frozen=True, slots=True)
class ToolAlias:
    """A namespace-only command alias targeting a mounted trusted tool."""

    target: str
    guest_path: str

    def validated(self) -> tuple[PurePosixPath, PurePosixPath]:
        trusted_root = PurePosixPath("/opt/rich-tools")
        alias_root = trusted_root / "bin"
        target = PurePosixPath(self.target)
        guest = PurePosixPath(self.guest_path)
        if (
            not target.is_absolute()
            or trusted_root not in target.parents
            or ".." in target.parts
        ):
            raise SandboxPolicyError(
                "tool alias targets must be below /opt/rich-tools"
            )
        if (
            not guest.is_absolute()
            or guest.parent != alias_root
            or guest.name in {"", ".", ".."}
            or ".." in guest.parts
        ):
            raise SandboxPolicyError(
                "tool aliases must be direct children of /opt/rich-tools/bin"
            )
        return target, guest


@dataclass(frozen=True, slots=True)
class TrustedNodePnpmRuntime:
    """Exact, read-only Node and pnpm tools exposed inside Bubblewrap."""

    executor: "BubblewrapExecutor"
    node_executable: str
    pnpm_script: str
    node_version: str = TRUSTED_NODE_VERSION
    pnpm_version: str = TRUSTED_PNPM_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.executor, BubblewrapExecutor):
            raise TypeError("executor must be a BubblewrapExecutor")
        for name in ("node_executable", "pnpm_script"):
            value = getattr(self, name)
            path = PurePosixPath(value)
            if (
                not isinstance(value, str)
                or not path.is_absolute()
                or PurePosixPath("/opt/rich-tools") not in path.parents
                or ".." in path.parts
            ):
                raise SandboxPolicyError(
                    f"{name} must be an absolute trusted tool path"
                )
        if self.node_version != TRUSTED_NODE_VERSION:
            raise SandboxPolicyError(
                f"Node must be pinned to {TRUSTED_NODE_VERSION}"
            )
        if self.pnpm_version != TRUSTED_PNPM_VERSION:
            raise SandboxPolicyError(
                f"pnpm must be pinned to {TRUSTED_PNPM_VERSION}"
            )

    def pnpm_argv(self, *arguments: str) -> tuple[str, ...]:
        if any(
            not isinstance(argument, str)
            or not argument
            or "\x00" in argument
            for argument in arguments
        ):
            raise SandboxPolicyError("pnpm arguments must be non-empty strings")
        return (self.node_executable, self.pnpm_script, *arguments)

    def verification_argv(self, script: str) -> tuple[str, ...]:
        if not isinstance(script, str) or not script or "\x00" in script:
            raise SandboxPolicyError("verification script cannot be empty")
        return self.pnpm_argv("run", script)


@dataclass(frozen=True, slots=True)
class WorkspaceBootstrapResult:
    dependency_install: ExecutionResult
    browser_install: ExecutionResult | None

    @property
    def passed(self) -> bool:
        return self.dependency_install.passed and (
            self.browser_install is None or self.browser_install.passed
        )


@dataclass(frozen=True, slots=True)
class WorkspaceBootstrapper:
    """Install an immutable pnpm graph and Playwright Chromium in bounded sandboxes."""

    runtime: TrustedNodePnpmRuntime
    runtime_directory: str = ".rich/runtime"
    # A shared cache root on the host. Set, the pnpm store and the Playwright
    # browsers live there across runs -- mounted writable for this trusted
    # install only, and read-only for every gate -- so a second build does not
    # download the world again. Unset, both live inside the workspace.
    cache_root: str | Path | None = None
    # One budget covers both installs. A cold bootstrap of the Next.js pack
    # measured ~7 minutes for the frozen dependency graph (~1.6 GiB) and ~4 for
    # Chromium (~650 MiB), which overran the previous 600s ceiling on a healthy
    # connection. Failing a legitimate cold bootstrap is worse than tolerating a
    # slow one: the sandbox still enforces memory, process, file and CPU limits,
    # and this deadline remains hard.
    timeout_seconds: float = 1800
    max_output_bytes: int = 1024 * 1024
    max_network_concurrency: int = 8
    max_fetch_retries: int = 2

    def __post_init__(self) -> None:
        if not isinstance(self.runtime, TrustedNodePnpmRuntime):
            raise TypeError("runtime must be a TrustedNodePnpmRuntime")
        runtime_directory = _safe_relative(self.runtime_directory)
        if runtime_directory == PurePosixPath("."):
            raise SandboxPolicyError(
                "runtime_directory must be a workspace child"
            )
        for name in ("max_output_bytes", "max_network_concurrency", "max_fetch_retries"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < (0 if name == "max_fetch_retries" else 1)
            ):
                raise ValueError(f"{name} is outside its allowed range")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive")

    def bootstrap(
        self,
        workspace: str | Path,
        *,
        install_browser: bool = True,
        cancellation: Callable[[], bool] | None = None,
        deadline: float | None = None,
    ) -> WorkspaceBootstrapResult:
        _validate_process_controls(cancellation, deadline)
        bootstrap_deadline = time.monotonic() + self.timeout_seconds
        if deadline is not None:
            bootstrap_deadline = min(bootstrap_deadline, deadline)
        root = Path(workspace).resolve(strict=True)
        if not root.is_dir():
            raise WorkspaceBootstrapError("workspace must be a directory")
        package_paths = _locked_workspace_package_paths(root)
        runtime_path = str(_safe_relative(self.runtime_directory))
        store_path = f"{runtime_path}/pnpm-store"
        browser_path = f"{runtime_path}/playwright"
        cache_mounts: tuple[CacheMount, ...] = ()
        store_guest = f"/workspace/{store_path}"
        browsers_guest = f"/workspace/{browser_path}"
        if self.cache_root is not None:
            cache_mounts = cache_mounts_for(self.cache_root, writable=True)
            store_guest = CACHE_STORE_GUEST
            browsers_guest = CACHE_BROWSERS_GUEST
        writable_paths = tuple(
            dict.fromkeys(
                (
                    runtime_path,
                    "node_modules",
                    *(
                        f"{path}/node_modules"
                        for path in package_paths
                        if path != "."
                    ),
                )
            )
        )
        common_environment = {
            "CI": "1",
            "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
            # pnpm otherwise sizes its import worker pool from the host CPU
            # count. Each worker creates a V8 isolate whose virtual CodeRange
            # reservation counts against the sandbox's RLIMIT_AS, so a highly
            # parallel host can crash a valid install before physical memory is
            # exhausted. Keep the memory boundary and make imports deterministic.
            "PNPM_MAX_WORKERS": "1",
            # Node's WebAssembly trap handler reserves a large virtual guard
            # region for Undici's HTTP parser. Bounds-checked mode keeps browser
            # downloads compatible with a finite RLIMIT_AS on 64-bit hosts.
            "NODE_OPTIONS": (
                "--disable-wasm-trap-handler --max-old-space-size=1536"
            ),
            "PLAYWRIGHT_BROWSERS_PATH": browsers_guest,
            "PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT": "60000",
        }
        policy = SandboxPolicy(
            writable_paths=writable_paths,
            network=True,
            environment=common_environment,
            cache_mounts=cache_mounts,
            timeout_seconds=self.timeout_seconds,
            max_memory_bytes=3_221_225_472,
            max_file_bytes=536_870_912,
            max_processes=128,
            max_cpu_seconds=max(1, int(self.timeout_seconds)),
            max_output_bytes=self.max_output_bytes,
        )
        # Two bootstraps writing one cache at once could leave a half-installed
        # browser for the other to find; a lock on the cache root serializes
        # them. Bootstraps with no shared cache do not wait on each other.
        with _cache_lock(self.cache_root):
            return self._install(
                root, policy, store_guest, install_browser, cancellation, bootstrap_deadline
            )

    def _install(
        self,
        root: Path,
        policy: SandboxPolicy,
        store_guest: str,
        install_browser: bool,
        cancellation: Callable[[], bool] | None,
        bootstrap_deadline: float,
    ) -> WorkspaceBootstrapResult:
        dependency_install = self.runtime.executor.run(
            root,
            self.runtime.pnpm_argv(
                "install",
                "--frozen-lockfile",
                "--ignore-scripts",
                "--strict-peer-dependencies",
                "--verify-store-integrity",
                "--store-dir",
                store_guest,
                "--network-concurrency",
                str(self.max_network_concurrency),
                "--fetch-retries",
                str(self.max_fetch_retries),
                "--fetch-timeout",
                "60000",
                "--fetch-retry-maxtimeout",
                "60000",
            ),
            policy,
            cancellation=cancellation,
            deadline=bootstrap_deadline,
        )
        if not dependency_install.passed:
            state = (
                "canceled"
                if dependency_install.cancelled
                else (
                    "timed out"
                    if dependency_install.timed_out
                    else f"exited {dependency_install.returncode}"
                )
            )
            raise WorkspaceBootstrapError(f"frozen dependency install {state}")

        browser_install: ExecutionResult | None = None
        if install_browser:
            browser_install = self.runtime.executor.run(
                root,
                self.runtime.pnpm_argv(
                    "exec",
                    "playwright",
                    "install",
                    "chromium",
                ),
                policy,
                cancellation=cancellation,
                deadline=bootstrap_deadline,
            )
            if not browser_install.passed:
                state = (
                    "canceled"
                    if browser_install.cancelled
                    else (
                        "timed out"
                        if browser_install.timed_out
                        else f"exited {browser_install.returncode}"
                    )
                )
                raise WorkspaceBootstrapError(f"browser install {state}")
        return WorkspaceBootstrapResult(dependency_install, browser_install)

    def wait_for_idle(self, timeout: float | None = None) -> bool:
        """Wait until the trusted executor owns no live process group."""

        return self.runtime.executor.wait_for_idle(timeout)


@contextlib.contextmanager
def _cache_lock(cache_root: str | Path | None) -> Iterator[None]:
    if cache_root is None:
        yield
        return
    root = Path(cache_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".bootstrap.lock").open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise SandboxPolicyError(f"path must remain inside the workspace: {value!r}")
    return path


class BubblewrapExecutor:
    """Execute an argv vector with an explicit filesystem and network policy."""

    def __init__(
        self,
        executable: str | None = None,
        *,
        tool_mounts: Sequence[ToolMount] = (),
        tool_aliases: Sequence[ToolAlias] = (),
        poll_interval_seconds: float = 0.02,
        termination_grace_seconds: float = 0.25,
    ):
        for name, value in (
            ("poll_interval_seconds", poll_interval_seconds),
            ("termination_grace_seconds", termination_grace_seconds),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be positive")
        self.executable = executable or shutil.which("bwrap")
        self.tool_mounts = tuple(tool_mounts)
        self.tool_aliases = tuple(tool_aliases)
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.termination_grace_seconds = float(termination_grace_seconds)
        self._process_condition = threading.Condition()
        self._active_process_groups: set[int] = set()
        self._active_launches = 0

    def available(self) -> bool:
        return bool(self.executable and Path(self.executable).is_file())

    def command(
        self,
        workspace: str | Path,
        argv: Sequence[str],
        policy: SandboxPolicy,
    ) -> list[str]:
        if not self.available():
            raise SandboxUnavailable("Bubblewrap is required but was not found")
        if not argv or any(
            not isinstance(item, str) or not item or "\x00" in item
            for item in argv
        ):
            raise SandboxPolicyError("argv must contain non-empty, NUL-free strings")
        policy.validate()
        root = Path(workspace).resolve(strict=True)
        if not root.is_dir():
            raise SandboxPolicyError(f"workspace is not a directory: {root}")

        workdir = _workspace_path(
            root,
            _safe_relative(policy.working_directory),
            create=False,
        )
        if not workdir.is_dir():
            raise SandboxPolicyError(
                f"working directory does not exist: {policy.working_directory!r}"
            )

        command = [
            str(self.executable),
            "--die-with-parent",
            "--new-session",
            "--unshare-user",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--unshare-cgroup-try",
            "--disable-userns",
            "--ro-bind",
            "/usr",
            "/usr",
        ]
        for system_path in ("/bin", "/sbin", "/lib", "/lib64"):
            if Path(system_path).exists():
                command.extend(["--ro-bind", system_path, system_path])
        if Path("/sys/devices/system/cpu").is_dir():
            command.extend(
                [
                    "--dir",
                    "/sys",
                    "--dir",
                    "/sys/devices",
                    "--dir",
                    "/sys/devices/system",
                    "--ro-bind",
                    "/sys/devices/system/cpu",
                    "/sys/devices/system/cpu",
                ]
            )
        if Path("/etc/fonts").is_dir():
            command.extend(["--dir", "/etc", "--ro-bind", "/etc/fonts", "/etc/fonts"])
        command.extend(
            [
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--tmpfs",
                "/tmp",
                "--dir",
                "/run",
                "--dir",
                "/opt",
                "--dir",
                "/opt/rich-tools",
                "--ro-bind",
                str(root),
                "/workspace",
            ]
        )

        for mount in self.tool_mounts:
            host_path, guest_path = mount.validated()
            command.extend(["--ro-bind", str(host_path), str(guest_path)])

        if self.tool_aliases:
            command.extend(["--dir", "/opt/rich-tools/bin"])
            for alias in self.tool_aliases:
                target, guest_path = alias.validated()
                command.extend(["--symlink", str(target), str(guest_path)])

        if policy.cache_mounts:
            command.extend(["--dir", str(CACHE_GUEST_ROOT)])
            for cache in policy.cache_mounts:
                host_path, guest_path = cache.validated()
                command.extend(
                    ["--bind" if cache.writable else "--ro-bind", str(host_path), str(guest_path)]
                )

        mounted: set[PurePosixPath] = set()
        for relative in sorted(
            {_safe_relative(path) for path in policy.writable_paths},
            key=lambda path: (len(path.parts), str(path)),
        ):
            if any(parent in mounted for parent in relative.parents):
                continue
            host_path = _workspace_path(root, relative, create=True)
            guest_path = PurePosixPath("/workspace").joinpath(relative)
            command.extend(["--bind", str(host_path), str(guest_path)])
            mounted.add(relative)

        if not policy.network:
            command.append("--unshare-net")
        else:
            if not Path("/etc/fonts").is_dir():
                command.extend(["--dir", "/etc"])
            for system_path in (
                "/etc/hosts",
                "/etc/nsswitch.conf",
                "/etc/resolv.conf",
                "/etc/ssl",
                "/etc/ca-certificates",
            ):
                if Path(system_path).exists():
                    command.extend(["--ro-bind", system_path, system_path])
        guest_cwd = PurePosixPath("/workspace").joinpath(
            _safe_relative(policy.working_directory)
        )
        command.extend(["--chdir", str(guest_cwd), "--"])
        command.extend(argv)
        return command

    def run(
        self,
        workspace: str | Path,
        argv: Sequence[str],
        policy: SandboxPolicy | None = None,
        *,
        cancellation: Callable[[], bool] | None = None,
        deadline: float | None = None,
    ) -> ExecutionResult:
        _validate_process_controls(cancellation, deadline)
        policy = policy or SandboxPolicy()
        command = self.command(workspace, argv, policy)
        environment = {
            "PATH": ":".join(
                [
                    *(["/opt/rich-tools/bin"] if self.tool_aliases else []),
                    *[
                        f"{mount.validated()[1]}/bin"
                        for mount in self.tool_mounts
                        if (mount.validated()[0] / "bin").is_dir()
                    ],
                    "/usr/bin",
                ]
            ),
            "HOME": "/tmp/rich-home",
            "TMPDIR": "/tmp",
            "PYTHONDONTWRITEBYTECODE": "1",
            **dict(policy.environment),
        }
        # RLIMIT_NPROC is accounted per host UID, not per Bubblewrap namespace.
        # Preserve room for already-running host threads while still capping the
        # sandbox's additional process allowance.
        _, nproc_hard = resource.getrlimit(resource.RLIMIT_NPROC)
        desired_nproc = _current_user_threads() + policy.max_processes
        process_limit = (
            desired_nproc
            if nproc_hard == resource.RLIM_INFINITY
            else min(desired_nproc, nproc_hard)
        )

        def apply_limits() -> None:
            resource.setrlimit(
                resource.RLIMIT_AS, (policy.max_memory_bytes, policy.max_memory_bytes)
            )
            resource.setrlimit(
                resource.RLIMIT_FSIZE, (policy.max_file_bytes, policy.max_file_bytes)
            )
            resource.setrlimit(
                resource.RLIMIT_NPROC, (process_limit, process_limit)
            )
            resource.setrlimit(
                resource.RLIMIT_CPU, (policy.max_cpu_seconds, policy.max_cpu_seconds)
            )

        started = time.monotonic()
        command_deadline = started + policy.timeout_seconds
        if deadline is not None:
            command_deadline = min(command_deadline, deadline)
        if cancellation is not None and cancellation():
            return ExecutionResult(
                argv=tuple(argv),
                returncode=-1,
                stdout="",
                stderr="",
                duration_seconds=time.monotonic() - started,
                cancelled=True,
            )
        if time.monotonic() >= command_deadline:
            return ExecutionResult(
                argv=tuple(argv),
                returncode=-1,
                stdout="",
                stderr="",
                duration_seconds=time.monotonic() - started,
                timed_out=True,
            )

        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            process: subprocess.Popen[bytes] | None = None
            process_group: int | None = None
            timed_out = False
            cancelled = False
            self._begin_process_launch()
            try:
                process = subprocess.Popen(
                    command,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    preexec_fn=apply_limits,
                    env=environment,
                    start_new_session=True,
                )
                process_group = process.pid
                self._register_process_group(process_group)
                while process.poll() is None:
                    if cancellation is not None and cancellation():
                        cancelled = True
                        break
                    remaining = command_deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        break
                    time.sleep(min(self.poll_interval_seconds, remaining))

                if cancelled or timed_out:
                    _terminate_process_group(
                        process,
                        process_group,
                        poll_interval_seconds=self.poll_interval_seconds,
                        grace_seconds=self.termination_grace_seconds,
                    )
                else:
                    process.wait()
                    # A command that daemonizes work has not completed within
                    # its sandbox contract. Tear down the inherited group before
                    # returning so no verifier or bootstrap process survives.
                    if _live_process_group_members(process_group):
                        _terminate_process_group(
                            process,
                            process_group,
                            poll_interval_seconds=self.poll_interval_seconds,
                            grace_seconds=self.termination_grace_seconds,
                        )
                        timed_out = True
                return ExecutionResult(
                    argv=tuple(argv),
                    returncode=(
                        process.returncode
                        if process.returncode is not None
                        else -signal.SIGKILL
                    ),
                    stdout=_bounded_output(stdout_file, policy.max_output_bytes),
                    stderr=_bounded_output(stderr_file, policy.max_output_bytes),
                    duration_seconds=time.monotonic() - started,
                    timed_out=timed_out,
                    cancelled=cancelled,
                )
            except PermissionError as exc:
                raise SandboxUnavailable(
                    "Bubblewrap exists but this host does not permit user namespaces"
                ) from exc
            except BaseException:
                if process is not None and process_group is not None:
                    _terminate_process_group(
                        process,
                        process_group,
                        poll_interval_seconds=self.poll_interval_seconds,
                        grace_seconds=self.termination_grace_seconds,
                    )
                raise
            finally:
                if process_group is not None:
                    self._unregister_process_group(process_group)
                self._finish_process_launch()

    def wait_for_idle(self, timeout: float | None = None) -> bool:
        """Wait for all process groups launched by this executor to be reaped."""

        if timeout is not None and (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout < 0
        ):
            raise ValueError("timeout must be non-negative when provided")
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._process_condition:
            while self._active_launches or self._active_process_groups:
                remaining = (
                    None
                    if deadline is None
                    else max(0.0, deadline - time.monotonic())
                )
                if remaining == 0:
                    return False
                self._process_condition.wait(remaining)
            return True

    def _register_process_group(self, process_group: int) -> None:
        with self._process_condition:
            self._active_process_groups.add(process_group)

    def _begin_process_launch(self) -> None:
        with self._process_condition:
            self._active_launches += 1

    def _finish_process_launch(self) -> None:
        with self._process_condition:
            self._active_launches -= 1
            self._process_condition.notify_all()

    def _unregister_process_group(self, process_group: int) -> None:
        with self._process_condition:
            self._active_process_groups.discard(process_group)
            self._process_condition.notify_all()


def _workspace_path(
    root: Path,
    relative: PurePosixPath,
    *,
    create: bool,
) -> Path:
    """Resolve a workspace path without following attacker-controlled symlinks."""

    candidate = root.joinpath(*relative.parts)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise SandboxPolicyError(
                f"workspace path crosses a symbolic link: {relative!s}"
            )
        if not current.exists():
            break
    if create and not candidate.exists():
        candidate.mkdir(parents=True, exist_ok=True)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (FileNotFoundError, ValueError) as exc:
        raise SandboxPolicyError("workspace path escapes workspace") from exc
    return resolved


def _bounded_output(stream: object, limit: int) -> str:
    stream.seek(0)  # type: ignore[attr-defined]
    content = stream.read(limit + 1)  # type: ignore[attr-defined]
    truncated = len(content) > limit
    decoded = content[:limit].decode("utf-8", errors="replace")
    if truncated:
        decoded += "\n[RICH output truncated]"
    return decoded


def _validate_process_controls(
    cancellation: Callable[[], bool] | None,
    deadline: float | None,
) -> None:
    if cancellation is not None and not callable(cancellation):
        raise TypeError("cancellation must be callable when provided")
    if deadline is not None and (
        isinstance(deadline, bool)
        or not isinstance(deadline, (int, float))
        or not math.isfinite(deadline)
    ):
        raise ValueError("deadline must be a finite monotonic timestamp")


def _terminate_process_group(
    process: subprocess.Popen[bytes],
    process_group: int,
    *,
    poll_interval_seconds: float,
    grace_seconds: float,
) -> None:
    """Terminate and reap one isolated process group, escalating to SIGKILL."""

    _signal_process_group(process_group, signal.SIGTERM)
    _wait_for_process_group(
        process,
        process_group,
        timeout=grace_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    if _live_process_group_members(process_group):
        _signal_process_group(process_group, signal.SIGKILL)
        _wait_for_process_group(
            process,
            process_group,
            timeout=grace_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
    if process.poll() is None:
        process.kill()
    process.wait()
    if _live_process_group_members(process_group):
        raise SandboxError(
            "sandbox process group remained live after SIGKILL"
        )


def _signal_process_group(process_group: int, requested_signal: int) -> None:
    try:
        os.killpg(process_group, requested_signal)
    except ProcessLookupError:
        return


def _wait_for_process_group(
    process: subprocess.Popen[bytes],
    process_group: int,
    *,
    timeout: float,
    poll_interval_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout
    while _live_process_group_members(process_group):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            process.wait(timeout=min(poll_interval_seconds, remaining))
        except subprocess.TimeoutExpired:
            pass
        if process.poll() is not None:
            time.sleep(min(poll_interval_seconds, remaining))


def _live_process_group_members(process_group: int) -> tuple[int, ...]:
    """Return non-zombie Linux processes in a process group."""

    members: list[int] = []
    try:
        entries = tuple(Path("/proc").iterdir())
    except OSError:
        # Bubblewrap is Linux-only. If procfs becomes unavailable, killpg(0)
        # is the conservative fallback and treats an extant group as live.
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            return ()
        return (process_group,)
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            stat_line = (entry / "stat").read_text(errors="replace")
            fields = stat_line[stat_line.rfind(")") + 2 :].split()
            state = fields[0]
            group = int(fields[2])
        except (OSError, ValueError, IndexError):
            continue
        if group == process_group and state != "Z":
            members.append(int(entry.name))
    return tuple(members)


def _current_user_threads() -> int:
    """Count host threads for this UID before lowering RLIMIT_NPROC."""

    uid = os.getuid()
    total = 0
    proc = Path("/proc")
    try:
        entries = tuple(proc.iterdir())
    except OSError:
        return 0
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            status = (entry / "status").read_text(errors="replace")
        except OSError:
            continue
        owner: int | None = None
        threads = 1
        for line in status.splitlines():
            if line.startswith("Uid:"):
                owner = int(line.split()[1])
            elif line.startswith("Threads:"):
                threads = int(line.split()[1])
        if owner == uid:
            total += threads
    return total


def trusted_node_pnpm_runtime(
    *,
    node_root: str | Path | None = None,
    pnpm_root: str | Path | None = None,
    bubblewrap_executable: str | None = None,
) -> TrustedNodePnpmRuntime:
    """Resolve the exact locally provisioned toolchain without downloading it.

    Both roots are mounted read-only. Missing tools, version drift, and malformed
    package metadata are hard errors; this function never falls back to an
    unpinned system package manager.
    """

    if node_root is None:
        discovered = shutil.which("node")
        if not discovered:
            raise SandboxUnavailable(
                f"trusted Node {TRUSTED_NODE_VERSION} is not installed"
            )
        try:
            resolved_node = Path(discovered).resolve(strict=True)
        except OSError as exc:
            raise SandboxUnavailable("trusted Node executable cannot be resolved") from exc
        resolved_node_root = resolved_node.parent.parent
    else:
        try:
            resolved_node_root = Path(node_root).resolve(strict=True)
        except OSError as exc:
            raise SandboxUnavailable("trusted Node root cannot be resolved") from exc

    node = resolved_node_root / "bin" / "node"
    header = resolved_node_root / "include" / "node" / "node_version.h"
    try:
        resolved_node = node.resolve(strict=True)
        resolved_node.relative_to(resolved_node_root)
        if not resolved_node.is_file():
            raise OSError("Node executable is not a file")
        resolved_header = header.resolve(strict=True)
        resolved_header.relative_to(resolved_node_root)
        header_content = _bounded_file_bytes(
            resolved_header, _MAX_MANIFEST_BYTES
        ).decode("utf-8")
    except (
        OSError,
        UnicodeDecodeError,
        ValueError,
        SandboxPolicyError,
    ) as exc:
        raise SandboxUnavailable(
            "trusted Node distribution is incomplete or escapes its root"
        ) from exc
    discovered_version = _node_header_version(header_content)
    if discovered_version != TRUSTED_NODE_VERSION:
        raise SandboxUnavailable(
            f"trusted Node version mismatch: expected {TRUSTED_NODE_VERSION}, "
            f"found {discovered_version}"
        )

    if pnpm_root is None:
        corepack_home = os.environ.get("COREPACK_HOME")
        if corepack_home:
            cache_root = Path(corepack_home)
        else:
            xdg_cache = os.environ.get("XDG_CACHE_HOME")
            cache_root = (
                Path(xdg_cache) / "node" / "corepack"
                if xdg_cache
                else Path.home() / ".cache" / "node" / "corepack"
            )
        candidate_pnpm_root = (
            cache_root / "v1" / "pnpm" / TRUSTED_PNPM_VERSION
        )
    else:
        candidate_pnpm_root = Path(pnpm_root)
    try:
        resolved_pnpm_root = candidate_pnpm_root.resolve(strict=True)
        manifest_path = (
            resolved_pnpm_root / "package.json"
        ).resolve(strict=True)
        manifest_path.relative_to(resolved_pnpm_root)
        manifest_content = _bounded_file_bytes(
            manifest_path, _MAX_MANIFEST_BYTES
        )
        manifest = json.loads(manifest_content)
        pnpm_script = (resolved_pnpm_root / "bin" / "pnpm.cjs").resolve(strict=True)
        pnpm_script.relative_to(resolved_pnpm_root)
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        SandboxPolicyError,
    ) as exc:
        raise SandboxUnavailable(
            f"trusted pnpm {TRUSTED_PNPM_VERSION} is not cached correctly"
        ) from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("name") != "pnpm"
        or manifest.get("version") != TRUSTED_PNPM_VERSION
        or not pnpm_script.is_file()
    ):
        raise SandboxUnavailable(
            f"trusted pnpm {TRUSTED_PNPM_VERSION} failed identity validation"
        )

    guest_node_root = "/opt/rich-tools/node"
    guest_pnpm_root = "/opt/rich-tools/pnpm"
    executor = BubblewrapExecutor(
        executable=bubblewrap_executable,
        tool_mounts=(
            ToolMount(resolved_node_root, guest_node_root),
            ToolMount(resolved_pnpm_root, guest_pnpm_root),
        ),
        tool_aliases=(
            ToolAlias(
                f"{guest_pnpm_root}/bin/pnpm.cjs",
                "/opt/rich-tools/bin/pnpm",
            ),
        ),
    )
    return TrustedNodePnpmRuntime(
        executor=executor,
        node_executable=f"{guest_node_root}/bin/node",
        pnpm_script=f"{guest_pnpm_root}/bin/pnpm.cjs",
    )


def _bounded_file_bytes(path: Path, limit: int) -> bytes:
    try:
        size = path.stat().st_size
    except OSError:
        raise
    if size > limit:
        raise SandboxPolicyError(f"trusted metadata exceeds {limit} bytes")
    content = path.read_bytes()
    if len(content) != size:
        raise SandboxPolicyError("trusted metadata changed while being read")
    return content


def _node_header_version(content: str) -> str:
    fields = {
        name.lower(): value
        for name, value in _NODE_VERSION_DEFINE.findall(content)
    }
    if set(fields) != {"major", "minor", "patch"}:
        raise SandboxUnavailable("trusted Node version metadata is incomplete")
    return ".".join(fields[name] for name in ("major", "minor", "patch"))


def _locked_workspace_package_paths(root: Path) -> tuple[str, ...]:
    try:
        package_manifest_path = _workspace_path(
            root, PurePosixPath("package.json"), create=False
        )
        package_manifest = json.loads(
            _bounded_file_bytes(
                package_manifest_path, _MAX_MANIFEST_BYTES
            )
        )
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        SandboxPolicyError,
    ) as exc:
        raise WorkspaceBootstrapError(
            "workspace requires a bounded valid package.json"
        ) from exc
    if (
        not isinstance(package_manifest, dict)
        or package_manifest.get("packageManager")
        != f"pnpm@{TRUSTED_PNPM_VERSION}"
    ):
        raise WorkspaceBootstrapError(
            f"workspace must pin packageManager to pnpm@{TRUSTED_PNPM_VERSION}"
        )

    try:
        lockfile_path = _workspace_path(
            root, PurePosixPath("pnpm-lock.yaml"), create=False
        )
        lockfile_content = _bounded_file_bytes(
            lockfile_path, _MAX_LOCKFILE_BYTES
        ).decode("utf-8")
        lockfile = yaml.safe_load(lockfile_content)
    except (
        OSError,
        UnicodeDecodeError,
        yaml.YAMLError,
        ValueError,
        SandboxPolicyError,
    ) as exc:
        raise WorkspaceBootstrapError(
            "workspace requires a bounded valid pnpm-lock.yaml"
        ) from exc
    if not isinstance(lockfile, Mapping):
        raise WorkspaceBootstrapError("pnpm lockfile root must be a mapping")
    importers = lockfile.get("importers")
    if not isinstance(importers, Mapping) or not importers:
        raise WorkspaceBootstrapError("pnpm lockfile must declare importers")

    normalized: list[str] = []
    seen: set[str] = set()
    for raw_path in importers:
        if not isinstance(raw_path, str) or not raw_path:
            raise WorkspaceBootstrapError("lockfile importer paths must be strings")
        try:
            relative = _safe_relative(raw_path)
        except SandboxPolicyError as exc:
            raise WorkspaceBootstrapError("lockfile importer escapes workspace") from exc
        canonical = "." if relative == PurePosixPath(".") else relative.as_posix()
        if raw_path != canonical or canonical in seen:
            raise WorkspaceBootstrapError(
                "lockfile importer paths must be unique and canonical"
            )
        seen.add(canonical)
        normalized.append(canonical)
        if canonical == ".":
            continue
        try:
            package_root = _workspace_path(root, relative, create=False)
            package_json = _workspace_path(
                root, relative / "package.json", create=False
            )
        except SandboxPolicyError as exc:
            raise WorkspaceBootstrapError(
                f"lockfile importer is unsafe: {canonical!r}"
            ) from exc
        if not package_root.is_dir() or not package_json.is_file():
            raise WorkspaceBootstrapError(
                f"lockfile importer is missing package.json: {canonical!r}"
            )
    if "." not in seen:
        raise WorkspaceBootstrapError("pnpm lockfile must include the root importer")
    return tuple(sorted(normalized))
