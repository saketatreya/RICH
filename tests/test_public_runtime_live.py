import pytest

from richbuild.interview import AdaptiveInterview, InterviewState
from richbuild.run_engine import (
    AcceptanceCoverageContext,
    BubblewrapCommandRunner,
    VerificationCommand,
)
from richbuild.planner import plan_nextjs_architecture
from richbuild.runtime import default_run_runtime
from richbuild.target_packs.nextjs import (
    NextJsTargetPack,
    NextJsTargetPackConfig,
)


@pytest.mark.live
def test_fresh_approved_scaffold_passes_the_pinned_sandbox_pipeline(tmp_path):
    project = AdaptiveInterview(
        InterviewState(
            "project.live-runtime",
            "Live Runtime",
            answers={
                "goal": "Publish an accessible project launch checklist.",
                "audiences": ["technical founders"],
                "capabilities": [
                    {
                        "id": "req.checklist",
                        "title": "Launch checklist",
                        "statement": (
                            "A founder can review the approved launch checklist."
                        ),
                    }
                ],
                "quality_constraints": [
                    {
                        "id": "req.a11y",
                        "title": "Keyboard access",
                        "statement": (
                            "The checklist is operable with a keyboard."
                        ),
                    }
                ],
                "scenarios": [
                    {
                        "id": "scenario.checklist",
                        "title": "Review checklist",
                        "when": ["A founder opens the checklist."],
                        "then": ["The approved checklist is visible."],
                        "requirement_ids": ["req.checklist"],
                        "oracle": [
                            {"action": "open_requirement"},
                            {
                                "action": "assert_visible",
                                "locator": {
                                    "kind": "text",
                                    "value": "A founder can review the approved launch checklist.",
                                },
                            },
                        ],
                    },
                    {
                        "id": "scenario.keyboard",
                        "title": "Keyboard navigation",
                        "when": ["A founder uses only a keyboard."],
                        "then": ["The checklist remains operable."],
                        "requirement_ids": ["req.a11y"],
                        "oracle": [
                            {"action": "navigate", "value": "/"},
                            {
                                "action": "press",
                                "locator": {
                                    "kind": "role",
                                    "value": "link",
                                    "name": "Keyboard access",
                                },
                                "value": "Enter",
                            },
                            {
                                "action": "assert_visible",
                                "locator": {
                                    "kind": "text",
                                    "value": "The checklist is operable with a keyboard.",
                                },
                            },
                        ],
                    },
                ],
            },
        )
    ).compile()
    architecture = plan_nextjs_architecture(project).architecture
    workspace = tmp_path / "workspace"
    NextJsTargetPack(
        NextJsTargetPackConfig(
            project_name="live-runtime",
            project_spec=project,
            architecture=architecture,
        )
    ).scaffold(workspace)
    runtime = default_run_runtime(
        {
            "max_model_attempts": 1,
            "max_input_tokens": 32_000,
            "max_output_tokens": 8_000,
            "max_cost_usd": "2",
            "max_execution_seconds": 120,
        },
        event_sink=lambda _kind, _payload: None,
        api_key_source="not-used-by-this-test",
    )

    prepared = runtime.bootstrapper.bootstrap(workspace)
    assert prepared.passed

    runner = BubblewrapCommandRunner(runtime.executor, timeout_seconds=600)
    for command in (
        runtime.commands.lint_argv,
        runtime.commands.static_argv,
        runtime.commands.unit_argv,
        runtime.commands.build_argv,
        runtime.commands.acceptance_argv,
    ):
        result = runner.run(
            workspace,
            # The runner only needs the kind to classify the command result.
            VerificationCommand(
                (
                    "lint"
                    if command == runtime.commands.lint_argv
                    else "static"
                    if command == runtime.commands.static_argv
                    else "unit"
                    if command == runtime.commands.unit_argv
                    else "build"
                    if command == runtime.commands.build_argv
                    else "acceptance"
                ),
                command,
                expected_acceptance_scenario_ids=(
                    tuple(sorted(project.scenario_index))
                    if command == runtime.commands.acceptance_argv
                    else ()
                ),
                acceptance_context=(
                    AcceptanceCoverageContext(
                        run_id="run.live-runtime",
                        task_id="run.live-runtime:acceptance",
                        attempt=1,
                        nonce="a" * 64,
                    )
                    if command == runtime.commands.acceptance_argv
                    else None
                ),
            ),
        )
        assert result.passed, result.stdout + "\n" + result.stderr
