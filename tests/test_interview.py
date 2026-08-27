import pytest

from richbuild.interview import (
    QUESTION_INDEX,
    AdaptiveInterview,
    InterviewIncomplete,
    InterviewState,
)
from richbuild.models import AcceptanceAction, ProjectSpec


def _complete_answers():
    return {
        "goal": "A realtime team todo product with login and durable tasks",
        "audiences": ["technical founders", "small product teams"],
        "capabilities": [
            {
                "id": "req.todo.add",
                "title": "Add tasks",
                "statement": "A signed-in member can add a task to their team.",
            }
        ],
        "roles": ["Members edit tasks only in their own team."],
        "data_policy": ["Tasks persist until a member deletes them."],
        "concurrency_policy": ["Concurrent updates use last accepted version with conflict notice."],
        "quality_constraints": [
            {
                "id": "req.a11y",
                "title": "Keyboard access",
                "statement": "Every task action is keyboard accessible.",
            }
        ],
        "scenarios": [
            {
                "id": "scenario.todo.add",
                "title": "Member adds a task",
                "given": ["A member is signed in to team A."],
                "when": ["They add a task named Buy milk."],
                "then": ["Buy milk appears in team A after refresh."],
                "requirement_ids": ["req.todo.add"],
                "oracle": [
                    {"action": "navigate", "value": "/"},
                    {
                        "action": "fill",
                        "locator": {"kind": "label", "value": "New task"},
                        "value": "Buy milk",
                    },
                    {
                        "action": "click",
                        "locator": {
                            "kind": "role",
                            "value": "button",
                            "name": "Add task",
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
                "id": "scenario.a11y",
                "title": "Keyboard task creation",
                "when": ["A keyboard user focuses the new task control and submits."],
                "then": ["The task is created and focus moves to the task."],
                "requirement_ids": ["req.a11y"],
                "oracle": [
                    {"action": "navigate", "value": "/"},
                    {"action": "keyboard", "value": "Tab"},
                    {
                        "action": "assert_visible",
                        "locator": {"kind": "role", "value": "textbox"},
                    },
                ],
            },
        ],
    }


def test_interview_starts_with_outcome_audience_and_capabilities():
    interview = AdaptiveInterview(InterviewState("project.todo", "Todo"))

    assert [question.id for question in interview.next_questions()] == [
        "goal",
        "audiences",
        "capabilities",
    ]


def test_questions_adapt_to_identity_persistence_and_realtime():
    state = InterviewState(
        "project.todo",
        "Todo",
        answers={
            "goal": "Realtime team todo with user login and persistent tasks",
            "audiences": ["teams"],
            "capabilities": [{"statement": "Collaborate on stored tasks"}],
        },
    )

    ids = [
        question.id
        for question in AdaptiveInterview(state).next_questions(limit=10)
    ]

    assert {"roles", "data_policy", "concurrency_policy"} <= set(ids)
    assert "integration_failure_policy" not in ids


def test_compile_produces_fully_traced_project_spec():
    state = InterviewState(
        "project.todo", "Todo", answers=_complete_answers(), revision=7
    )

    spec = AdaptiveInterview(state).compile()

    assert spec.revision == 7
    assert set(spec.requirement_index) == {"req.todo.add", "req.a11y"}
    assert spec.metadata["discovery"]["roles"]
    assert all(scenario.requirement_ids for scenario in spec.acceptance_scenarios)
    assert "oracle:list" in QUESTION_INDEX["scenarios"].answer_kind
    assert spec.acceptance_scenarios[0].oracle[0].action is AcceptanceAction.NAVIGATE
    assert ProjectSpec.from_dict(spec.to_dict()) == spec


def test_compile_refuses_requirement_without_acceptance_oracle():
    answers = _complete_answers()
    answers["scenarios"] = answers["scenarios"][:1]

    with pytest.raises(InterviewIncomplete, match="req.a11y"):
        AdaptiveInterview(
            InterviewState("project.todo", "Todo", answers=answers)
        ).compile()
