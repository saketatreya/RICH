import json

import pytest

from richbuild.cli import main


def test_doctor_reports_fail_closed_sandbox_policy(capsys):
    assert main(["doctor"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["sandbox"]["provider"] == "bubblewrap"
    assert output["sandbox"]["network_default"] == "denied"
    assert output["sandbox"]["unsafe_fallback"] is False


def test_project_create_and_show_use_selected_state_directory(tmp_path, capsys):
    state = tmp_path / "state"

    assert (
        main(
            [
                "--state-dir",
                str(state),
                "project-create",
                "project.demo",
                "Demo",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        main(
            [
                "--state-dir",
                str(state),
                "project-show",
                "project.demo",
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["id"] == "project.demo"
    assert output["current_revision"] == 0


def test_cli_returns_machine_readable_error(tmp_path, capsys):
    result = main(
        [
            "--state-dir",
            str(tmp_path / "state"),
            "project-show",
            "missing",
        ]
    )

    assert result == 2
    error = json.loads(capsys.readouterr().err)
    assert error["error"] == "NotFoundError"


@pytest.mark.parametrize(
    "argv",
    [
        ["--state-dir", "/tmp/rich-x", "doctor"],
        ["doctor", "--state-dir", "/tmp/rich-x"],
    ],
)
def test_state_dir_is_accepted_on_either_side_of_the_subcommand(argv):
    """`rich serve --state-dir X` is what a person types. Being told that is
    "unrecognized" is a bad first minute with a tool."""

    from richbuild.cli import _parser

    namespace = _parser().parse_args(argv)

    assert str(namespace.state_dir) == "/tmp/rich-x"


def test_omitting_it_leaves_the_default_to_the_caller():
    from richbuild.cli import _parser

    namespace = _parser().parse_args(["doctor"])

    assert getattr(namespace, "state_dir", None) is None


def test_doctor_reports_every_check_with_a_remedy(capsys, monkeypatch):
    """Two findings in one day were a sandbox the host would not run and a
    Node that had drifted a patch version; both were discovered by a build
    dying, not by the doctor. The doctor now runs the same probes a build does
    and says what to do about each miss. Secrets are present or absent, never
    shown."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("NEON_API_TOKEN", raising=False)
    monkeypatch.setenv("VERCEL_TOKEN", "a-token-value")

    assert main(["doctor"]) in (0, 1)

    output = json.loads(capsys.readouterr().out)
    checks = {entry["name"]: entry for entry in output["checks"]}
    assert {"python", "git", "sandbox", "toolchain", "route claude-code", "route api",
            "preview neon", "preview vercel", "canvas"} <= set(checks)
    assert checks["python"]["ok"] and checks["python"]["required"]
    assert checks["route api"]["ok"] is False and "export ANTHROPIC_API_KEY" in checks["route api"]["remedy"]
    assert checks["preview neon"]["ok"] is False and "NEON_API_TOKEN" in checks["preview neon"]["remedy"]
    assert checks["preview vercel"]["ok"] is True
    assert "a-token-value" not in json.dumps(output)
    for entry in output["checks"]:
        if not entry["ok"]:
            assert entry["remedy"], entry["name"]
    assert output["ok"] == all(entry["ok"] for entry in output["checks"] if entry["required"])


def test_version_is_reported(capsys):
    from richbuild import __version__

    with pytest.raises(SystemExit) as stop:
        main(["--version"])
    assert stop.value.code == 0
    assert __version__ in capsys.readouterr().out
    assert __version__ and __version__ != ""


def test_push_repository_lands_the_verified_snapshot_in_a_real_repository(tmp_path, capsys):
    import subprocess

    from richbuild.preview import create_deployment_snapshot
    from richbuild.store import RichStore

    state = tmp_path / "state"
    store = RichStore(state)
    project = store.create_project("Demo", project_id="project.cli-push")
    run = store.create_run(
        project["id"], spec_revision_id=None, architecture_revision_id=None, status="ready"
    )
    source = tmp_path / "generated"
    source.mkdir()
    (source / "README.md").write_text("verified by RICH\n")
    store.set_run_status(run["id"], "running", expected_status="ready")
    store.set_run_status(run["id"], "verifying", expected_status="running")
    store.set_run_status(run["id"], "succeeded", expected_status="verifying")
    release = store.put_artifact(
        create_deployment_snapshot(source),
        media_type="application/vnd.rich.release-source+zip",
    )
    store.attach_artifact(run["id"], release.digest, role="source:release-snapshot")
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)

    assert (
        main(["--state-dir", str(state), "push-repository", run["id"], bare.as_uri()])
        == 0
    )

    output = json.loads(capsys.readouterr().out)["push"]
    assert output["snapshot_digest"] == release.digest
    head = subprocess.run(
        ["git", "-C", str(bare), "rev-parse", "main"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert head == output["commit_sha"]
    shown = subprocess.run(
        ["git", "-C", str(bare), "show", "main:README.md"], capture_output=True, text=True, check=True
    ).stdout
    assert shown == "verified by RICH\n"
