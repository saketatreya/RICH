"""Push a verified release snapshot to a Git repository.

What lands in the repository is the run's content-addressed release snapshot
-- the ZIP the acceptance evidence is bound to -- never the working tree, so
it is exactly what was verified and cannot have drifted.  The commit is
deterministic: a fixed author, the run's finish time as both dates, and the
snapshot digest in the message.  Pushing the same run twice therefore yields
the same commit, and a later run's push lands on top of the branch it finds,
so a repository accumulates one commit per verified release.

Credentials never touch argv, the repository's configuration, or the disk in
the clear: ``git`` asks for them through ``GIT_ASKPASS``, a script that answers
from an environment variable set for that one subprocess, and every error
message is scrubbed of the token before it leaves.  Only ``https://`` and
``file://`` remotes are accepted; ``ssh`` would need an agent the server does
not have.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any, Callable
from urllib.parse import urlsplit

from .preview import (
    HttpTransport,
    ProviderApiError,
    UnsafeSourceTree,
    UrllibTransport,
    extract_deployment_snapshot,
)

GITHUB_TOKEN_HANDLE = "github.token"
_GITHUB_API = "https://api.github.com"
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_HTTP_STATUS_RE = re.compile(r"HTTP (\d{3})")
_URL_CREDENTIALS_RE = re.compile(r"(https?://)[^/@\s]+@")


class RepositoryError(RuntimeError):
    """A push that did not happen, with the reason and without the secret."""


class RepositoryRefused(RepositoryError, ValueError):
    """The request itself is not acceptable, before anything is attempted."""


@dataclass(frozen=True, slots=True)
class RepositoryTarget:
    """Where a snapshot goes.  Validated on construction; nothing is attempted."""

    remote: str
    branch: str = "main"
    token_handle: str | None = GITHUB_TOKEN_HANDLE
    create: bool = False
    private: bool = True

    def __post_init__(self) -> None:
        parts = urlsplit(self.remote)
        if parts.scheme not in {"https", "file"}:
            raise RepositoryRefused("remote must be an https:// or file:// URL")
        if parts.username or parts.password:
            raise RepositoryRefused(
                "remote must not carry credentials; they come from the secret handle"
            )
        if (
            not _BRANCH_RE.match(self.branch)
            or ".." in self.branch
            or "//" in self.branch
            or self.branch.endswith(("/", ".lock", "."))
        ):
            raise RepositoryRefused("branch name is not acceptable")
        if parts.scheme == "https" and self.token_handle is None:
            raise RepositoryRefused("an https:// remote needs a token handle")
        if self.token_handle is not None and self.token_handle != GITHUB_TOKEN_HANDLE:
            raise RepositoryRefused(f"unsupported secret handle {self.token_handle!r}")
        # A github.com remote must name <owner>/<repository>, whether or not
        # it is to be created; anything else is refused before it is tried.
        located = self.github_repository()
        if self.create and located is None:
            raise RepositoryRefused("only a github.com repository can be created")

    def github_repository(self) -> tuple[str, str] | None:
        parts = urlsplit(self.remote)
        if parts.scheme != "https" or (parts.hostname or "").lower() != "github.com":
            return None
        path = parts.path.strip("/")
        if path.endswith(".git"):
            path = path[:-4]
        pieces = path.split("/")
        if len(pieces) != 2 or not all(_SLUG_RE.match(piece) for piece in pieces):
            raise RepositoryRefused(
                "a github.com remote is https://github.com/<owner>/<repository>"
            )
        return pieces[0], pieces[1]

    @property
    def repository_url(self) -> str | None:
        located = self.github_repository()
        if located is None:
            return None
        return f"https://github.com/{located[0]}/{located[1]}"


@dataclass(frozen=True, slots=True)
class RepositoryPush:
    """What a push left behind, as recorded on the run."""

    run_id: str
    remote: str
    branch: str
    commit_sha: str
    snapshot_digest: str
    file_count: int
    committed_at: str
    repository_url: str | None
    created_repository: bool
    already_current: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "remote": self.remote,
            "branch": self.branch,
            "commit_sha": self.commit_sha,
            "snapshot_digest": self.snapshot_digest,
            "file_count": self.file_count,
            "committed_at": self.committed_at,
            "repository_url": self.repository_url,
            "created_repository": self.created_repository,
            "already_current": self.already_current,
        }


def _status_of(exc: ProviderApiError) -> int | None:
    match = _HTTP_STATUS_RE.search(str(exc))
    return int(match.group(1)) if match else None


def ensure_github_repository(
    owner: str,
    name: str,
    *,
    token: str,
    private: bool,
    transport: HttpTransport,
    api_base: str = _GITHUB_API,
) -> bool:
    """Create ``owner/name`` if it does not exist.  Returns whether it was created.

    A repository that already exists is left alone -- "create if missing" is
    what a customer who ticks the box means, and it is what makes a retried
    request after a crash mid-way land in the same place.
    """

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "rich",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        transport.request("GET", f"{api_base}/repos/{owner}/{name}", headers=headers)
        return False
    except ProviderApiError as exc:
        if _status_of(exc) != 404:
            raise RepositoryError(
                f"GitHub could not say whether {owner}/{name} exists: {exc}"
            ) from exc
    try:
        me = transport.request("GET", f"{api_base}/user", headers=headers).json()
    except ProviderApiError as exc:
        raise RepositoryError(f"GitHub did not accept the token: {exc}") from exc
    login = str(me.get("login") or "")
    url = (
        f"{api_base}/user/repos"
        if login.lower() == owner.lower()
        else f"{api_base}/orgs/{owner}/repos"
    )
    body = json.dumps(
        {
            "name": name,
            "private": private,
            "auto_init": False,
            "description": "Built and verified by RICH",
        }
    ).encode("utf-8")
    try:
        transport.request(
            "POST",
            url,
            headers={**headers, "Content-Type": "application/json"},
            body=body,
        )
    except ProviderApiError as exc:
        raise RepositoryError(
            f"GitHub refused to create {owner}/{name}: {exc}"
        ) from exc
    return True


def _scrub(text: str, token: str | None) -> str:
    scrubbed = _URL_CREDENTIALS_RE.sub(r"\1[REDACTED]@", text)
    if token:
        scrubbed = scrubbed.replace(token, "[REDACTED_SECRET]")
    return scrubbed


_ASKPASS = (
    "#!/bin/sh\n"
    'case "$1" in\n'
    "  *sername*) echo x-access-token ;;\n"
    '  *) printf %s "$RICH_GIT_TOKEN" ;;\n'
    "esac\n"
)


def push_snapshot(
    snapshot: bytes,
    *,
    run_id: str,
    snapshot_digest: str,
    target: RepositoryTarget,
    committed_at: datetime,
    secret_resolver: Callable[[str], str] | None = None,
    transport: HttpTransport | None = None,
    git: str = "git",
    timeout: float = 300.0,
) -> RepositoryPush:
    """Push one verified snapshot as one commit on ``target.branch``."""

    token: str | None = None
    if target.token_handle is not None:
        if secret_resolver is None:
            raise RepositoryError("no secret resolver is configured for the token")
        token = secret_resolver(target.token_handle)
        if not token:
            raise RepositoryError(
                f"secret handle {target.token_handle!r} resolved to an empty value"
            )
    if shutil.which(git) is None:
        raise RepositoryError(f"{git!r} is not on PATH")
    if committed_at.tzinfo is None:
        committed_at = committed_at.replace(tzinfo=timezone.utc)
    stamp = committed_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")

    created = False
    located = target.github_repository()
    if target.create and located is not None:
        assert token is not None  # an https target always carries a handle
        created = ensure_github_repository(
            located[0],
            located[1],
            token=token,
            private=target.private,
            transport=transport or UrllibTransport(),
        )

    with tempfile.TemporaryDirectory(prefix="rich-push-") as scratch:
        root = Path(scratch)
        try:
            tree = extract_deployment_snapshot(snapshot, root / "tree")
        except UnsafeSourceTree as exc:
            raise RepositoryError(f"release snapshot cannot be pushed: {exc}") from exc
        file_count = sum(1 for path in tree.rglob("*") if path.is_file())
        askpass = root / "askpass.sh"
        askpass.write_text(_ASKPASS)
        askpass.chmod(0o700)
        (root / "gitconfig").write_text(
            "[credential]\n\thelper =\n[core]\n\tautocrlf = false\n"
        )
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(root),
            "LC_ALL": "C",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": str(askpass),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(root / "gitconfig"),
            "GIT_AUTHOR_NAME": "RICH",
            "GIT_AUTHOR_EMAIL": "rich@localhost",
            "GIT_COMMITTER_NAME": "RICH",
            "GIT_COMMITTER_EMAIL": "rich@localhost",
            "GIT_AUTHOR_DATE": stamp,
            "GIT_COMMITTER_DATE": stamp,
        }
        if token:
            env["RICH_GIT_TOKEN"] = token
        repo = root / "repo"
        repo.mkdir()

        def run(*argv: str) -> str:
            try:
                completed = subprocess.run(
                    [git, *argv],
                    cwd=repo,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise RepositoryError(
                    f"git {argv[0]} did not finish within {timeout:.0f}s"
                ) from exc
            if completed.returncode != 0:
                detail = (completed.stderr.strip() or completed.stdout.strip())[:2000]
                raise RepositoryError(
                    f"git {argv[0]} failed: {_scrub(detail, token)}"
                )
            return completed.stdout

        run("init", "-q", "-b", target.branch)
        run("remote", "add", "origin", target.remote)
        listed = run("ls-remote", "--heads", "origin", target.branch)
        branch_exists = bool(listed.strip())
        if branch_exists:
            # Land on top of what is there: a repository accumulates one
            # commit per verified release instead of refusing the second.
            run("fetch", "-q", "origin", f"refs/heads/{target.branch}")
            run("checkout", "-q", "-B", target.branch, "FETCH_HEAD")
            for entry in repo.iterdir():
                if entry.name == ".git":
                    continue
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
        shutil.copytree(tree, repo, dirs_exist_ok=True)
        run("add", "-A", "--", ".")
        already_current = False
        if branch_exists:
            # `diff --cached --quiet` exits 0 when nothing is staged: this
            # snapshot already heads the branch, byte for byte.
            staged = subprocess.run(
                [git, "diff", "--cached", "--quiet"],
                cwd=repo,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            ).returncode
            if staged not in (0, 1):
                raise RepositoryError("git diff could not compare the snapshot")
            already_current = staged == 0
        if not already_current:
            run(
                "commit",
                "-q",
                "--no-verify",
                "-m",
                f"RICH run {run_id}\n\nrelease snapshot sha256:{snapshot_digest}\n",
            )
            run("push", "-q", "origin", f"HEAD:refs/heads/{target.branch}")
        commit_sha = run("rev-parse", "HEAD").strip()
        confirmed = run("ls-remote", "--heads", "origin", target.branch)
        remote_sha = confirmed.split()[0] if confirmed.strip() else ""
        if remote_sha != commit_sha:
            raise RepositoryError(
                "the remote branch does not hold the pushed commit"
            )

    return RepositoryPush(
        run_id=run_id,
        remote=target.remote,
        branch=target.branch,
        commit_sha=commit_sha,
        snapshot_digest=snapshot_digest,
        file_count=file_count,
        committed_at=committed_at.astimezone(timezone.utc).isoformat(),
        repository_url=target.repository_url,
        created_repository=created,
        already_current=already_current,
    )
