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
