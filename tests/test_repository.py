"""The repository push: a verified snapshot becomes one commit, nothing else."""

from datetime import datetime, timezone
import io
import subprocess
import zipfile

import pytest

from richbuild.preview import HttpResponse, ProviderApiError, create_deployment_snapshot
from richbuild.repository import (
    RepositoryError,
    RepositoryRefused,
    RepositoryTarget,
    ensure_github_repository,
    push_snapshot,
)

FINISHED = datetime(2026, 8, 29, 14, 39, 3, tzinfo=timezone.utc)


def _snapshot(tmp_path, name, files):
    source = tmp_path / name
    source.mkdir()
    for relative, content in files.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return create_deployment_snapshot(source)


def _bare(tmp_path, name="remote.git"):
    bare = tmp_path / name
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    return bare


def _git(bare, *argv):
    return subprocess.run(
        ["git", "-C", str(bare), *argv], capture_output=True, text=True, check=True
    ).stdout


def test_a_push_lands_exactly_the_snapshot_as_one_dated_commit(tmp_path):
    snapshot = _snapshot(
        tmp_path,
        "one",
        {"package.json": '{"name":"demo"}\n', "apps/web/src/app/page.tsx": "export default 1\n"},
    )
    bare = _bare(tmp_path)
    target = RepositoryTarget(remote=bare.as_uri(), token_handle=None)

    push = push_snapshot(
        snapshot,
        run_id="run.one",
        snapshot_digest="a" * 64,
        target=target,
        committed_at=FINISHED,
    )

    assert push.file_count == 2
    assert push.already_current is False
    assert push.created_repository is False
    assert push.repository_url is None
    assert _git(bare, "rev-parse", "refs/heads/main").strip() == push.commit_sha
    subject = _git(bare, "log", "-1", "--format=%s%n%b%n%an <%ae>%n%aI", "main")
    assert "RICH run run.one" in subject
    assert "release snapshot sha256:" + "a" * 64 in subject
    assert "RICH <rich@localhost>" in subject
    assert "2026-08-29T14:39:03" in subject
    files = _git(bare, "ls-tree", "-r", "--name-only", "main").split()
    assert files == ["apps/web/src/app/page.tsx", "package.json"]
    assert _git(bare, "show", "main:package.json") == '{"name":"demo"}\n'


def test_the_same_run_pushed_twice_is_the_same_commit(tmp_path):
    snapshot = _snapshot(tmp_path, "same", {"README.md": "verified\n"})
    bare = _bare(tmp_path)
    target = RepositoryTarget(remote=bare.as_uri(), token_handle=None)
    first = push_snapshot(
        snapshot, run_id="run.same", snapshot_digest="b" * 64, target=target, committed_at=FINISHED
    )
    second = push_snapshot(
        snapshot, run_id="run.same", snapshot_digest="b" * 64, target=target, committed_at=FINISHED
    )
    assert second.commit_sha == first.commit_sha
    assert second.already_current is True
    assert _git(bare, "rev-list", "--count", "main").strip() == "1"


def test_a_later_release_lands_on_top_of_the_earlier_one(tmp_path):
    bare = _bare(tmp_path)
    target = RepositoryTarget(remote=bare.as_uri(), token_handle=None)
    first = push_snapshot(
        _snapshot(tmp_path, "v1", {"a.txt": "one\n", "gone.txt": "old\n"}),
        run_id="run.v1",
        snapshot_digest="1" * 64,
        target=target,
        committed_at=FINISHED,
    )
    second = push_snapshot(
        _snapshot(tmp_path, "v2", {"a.txt": "two\n", "new.txt": "new\n"}),
        run_id="run.v2",
        snapshot_digest="2" * 64,
        target=target,
        committed_at=FINISHED,
    )
    assert second.commit_sha != first.commit_sha
    assert _git(bare, "rev-parse", "main~1").strip() == first.commit_sha
    assert _git(bare, "ls-tree", "-r", "--name-only", "main").split() == ["a.txt", "new.txt"]
    assert _git(bare, "show", "main:a.txt") == "two\n"


def test_targets_are_refused_before_anything_is_attempted():
    with pytest.raises(RepositoryRefused, match="https:// or file://"):
        RepositoryTarget(remote="git@github.com:owner/repo.git")
    with pytest.raises(RepositoryRefused, match="credentials"):
        RepositoryTarget(remote="https://user:secret@github.com/owner/repo.git")
    with pytest.raises(RepositoryRefused, match="token handle"):
        RepositoryTarget(remote="https://github.com/owner/repo.git", token_handle=None)
    with pytest.raises(RepositoryRefused, match="unsupported secret handle"):
        RepositoryTarget(remote="https://github.com/owner/repo.git", token_handle="other")
    with pytest.raises(RepositoryRefused, match="branch"):
        RepositoryTarget(remote="file:///tmp/x.git", token_handle=None, branch="../main")
    with pytest.raises(RepositoryRefused, match="github.com repository can be created"):
        RepositoryTarget(remote="https://gitlab.com/owner/repo.git", create=True)
    with pytest.raises(RepositoryRefused, match="<owner>/<repository>"):
        RepositoryTarget(remote="https://github.com/only-owner")
    target = RepositoryTarget(remote="https://github.com/Owner/Repo.git")
    assert target.github_repository() == ("Owner", "Repo")
    assert target.repository_url == "https://github.com/Owner/Repo"


def test_the_token_never_appears_in_a_failure(tmp_path):
    snapshot = _snapshot(tmp_path, "secret", {"x.txt": "x\n"})
    target = RepositoryTarget(remote="https://127.0.0.1:9/owner/repo.git")
    token = "ghp_verySecretToken123"

    with pytest.raises(RepositoryError) as caught:
        push_snapshot(
            snapshot,
            run_id="run.secret",
            snapshot_digest="c" * 64,
            target=target,
            committed_at=FINISHED,
            secret_resolver=lambda handle: token,
            timeout=30,
        )

    assert token not in str(caught.value)
    assert "git ls-remote failed" in str(caught.value)


def test_an_unsafe_snapshot_is_refused(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../escape.txt", "no")
    bare = _bare(tmp_path)
    with pytest.raises(RepositoryError, match="cannot be pushed"):
        push_snapshot(
            buffer.getvalue(),
            run_id="run.unsafe",
            snapshot_digest="d" * 64,
            target=RepositoryTarget(remote=bare.as_uri(), token_handle=None),
            committed_at=FINISHED,
        )


class FakeGitHub:
    def __init__(self, *, existing=(), login="maya"):
        self.existing = set(existing)
        self.login = login
        self.requests = []

    def request(self, method, url, *, headers, body=None, timeout=60):
        self.requests.append((method, url, headers.get("Authorization"), body))
        if method == "GET" and "/repos/" in url:
            owner_name = url.rsplit("/repos/", 1)[1]
            if owner_name in self.existing:
                return HttpResponse(200, b'{"full_name": "%s"}' % owner_name.encode())
            raise ProviderApiError("provider request failed with HTTP 404: not found")
        if method == "GET" and url.endswith("/user"):
            return HttpResponse(200, b'{"login": "%s"}' % self.login.encode())
        if method == "POST":
            return HttpResponse(201, b'{"html_url": "https://github.com/x/y"}')
        raise AssertionError(url)


def test_create_if_missing_uses_the_right_endpoint_for_user_and_org():
    github = FakeGitHub()
    assert ensure_github_repository(
        "maya", "tracker", token="t", private=True, transport=github
    ) is True
    assert [r[1] for r in github.requests][-1].endswith("/user/repos")
    assert github.requests[-1][2] == "Bearer t"
    github = FakeGitHub()
    assert ensure_github_repository(
        "acme", "tracker", token="t", private=False, transport=github
    ) is True
    assert github.requests[-1][1].endswith("/orgs/acme/repos")
    assert b'"private": false' in github.requests[-1][3]


def test_an_existing_repository_is_left_alone():
    github = FakeGitHub(existing={"maya/tracker"})
    assert ensure_github_repository(
        "maya", "tracker", token="t", private=True, transport=github
    ) is False
    assert len(github.requests) == 1


def test_a_token_github_rejects_is_a_plain_error():
    class Rejecting(FakeGitHub):
        def request(self, method, url, **kw):
            raise ProviderApiError("provider request failed with HTTP 401: bad credentials")

    with pytest.raises(RepositoryError, match="could not say whether"):
        ensure_github_repository("maya", "x", token="t", private=True, transport=Rejecting())
