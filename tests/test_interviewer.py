"""The interviewer: an invalid step is unrepresentable, ids are derived, a turn never raises."""

from decimal import Decimal
import json

import pytest

from richbuild.budget import BudgetLedger, RunBudget, Usage
from richbuild.interview import AdaptiveInterview, InterviewState
from richbuild.interviewer import (
    InterviewProposalError,
    ModelInterviewer,
    answers_from_keys,
    form_fallback,
    interviewer_prompt,
    interviewer_response_schema,
    locator_schema,
    propose_interview,
    step_schema,
)
from richbuild.models import AcceptanceAction, AcceptanceStep, ModelValidationError
from richbuild.providers import ModelGateway, ModelResponse

PROVIDER = "anthropic-claude-code"


class _ScriptedProvider:
    """Answers each call with the next scripted payload, repeating the last."""

    name = PROVIDER

    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        payload = self.payloads.pop(0) if len(self.payloads) > 1 else self.payloads[0]
        return ModelResponse(
            text=json.dumps(payload),
            parsed=payload,
            provider=self.name,
            model=request.model,
            usage=Usage(
                model_attempts=1,
                input_tokens=10,
                output_tokens=10,
                cost_usd=Decimal("0.01"),
                execution_seconds=0.1,
            ),
        )


def _gateway(provider):
    return ModelGateway(
        [provider],
        BudgetLedger(
            RunBudget(
                max_model_attempts=8,
                max_input_tokens=400_000,
                max_output_tokens=200_000,
                max_cost_usd=Decimal("5"),
                max_execution_seconds=3_600,
            )
        ),
    )


_TRANSCRIPT = (
    {"role": "user", "text": "A todo list where I add items and they are still there after a reload."},
)


def _turn(provider, *, answers=None, max_attempts=2):
    return propose_interview(
        project_id="project.todo",
        project_name="Todo",
        transcript=_TRANSCRIPT,
        answers=answers,
        gateway=_gateway(provider),
        provider=PROVIDER,
        model="claude-sonnet-5",
        run_id="interviewer:project.todo",
        task_id="interviewer:project.todo:turn",
        correlation_id="interviewer:project.todo:1",
        max_cost_usd=Decimal("0.50"),
        max_attempts=max_attempts,
    )


def _complete(**overrides):
    answers = {
        "goal": "Keep a list of things to do that survives a reload.",
        "audiences": ["A person keeping a list"],
        "capabilities": [
            {"key": "add-item", "title": "Add an item", "statement": "A person adds an item and sees it in the list."}
        ],
        "quality_constraints": [
            {"key": "keyboard", "title": "Keyboard access", "statement": "Every action works with a keyboard."}
        ],
        "scenarios": [
            {
                "key": "add",
                "title": "Add an item",
                "requirement_keys": ["add-item"],
                "when": ["They type Buy milk and add it."],
                "then": ["Buy milk is in the list, even after a reload."],
                "oracle": [
                    {"action": "open_requirement"},
                    {"action": "fill", "locator": {"kind": "label", "value": "New item"}, "value": "Buy milk"},
                    {"action": "click", "locator": {"kind": "role", "value": "button", "name": "Add item"}},
                    {"action": "reload"},
                    {"action": "assert_visible", "locator": {"kind": "text", "value": "Buy milk"}},
                ],
            },
            {
                "key": "keyboard",
                "title": "Keyboard only",
                "requirement_keys": ["keyboard"],
                "when": ["They use only the keyboard."],
                "then": ["The item field takes focus first."],
                "oracle": [
                    {"action": "open_requirement"},
                    {"action": "keyboard", "value": "Tab"},
                    {"action": "assert_focused", "locator": {"kind": "label", "value": "New item"}},
                ],
            },
        ],
    }
    answers.update(overrides)
    return {"status": "complete", "summary": "A todo list that survives a reload.", "answers": answers}


# ── the schema mirrors the step constructor exactly ─────────────────────────


def _minimal_step(action):
    branch = step_schema(action)
    step = {"action": action.value}
    if "locator" in branch["required"]:
        step["locator"] = {"kind": "label", "value": "New item"}
    if "value" in branch["required"]:
        step["value"] = "/" if action in {AcceptanceAction.NAVIGATE, AcceptanceAction.ASSERT_URL} else "Buy milk"
    return step


@pytest.mark.parametrize("action", list(AcceptanceAction))
def test_every_action_has_one_branch_that_the_constructor_agrees_with(action):
    branch = step_schema(action)
    assert branch["properties"]["action"]["enum"] == [action.value]
    assert branch["additionalProperties"] is False
    assert AcceptanceStep.from_dict(_minimal_step(action)).action is action
    # A field the branch does not offer is a field the constructor refuses.
    if "value" not in branch["properties"]:
        with pytest.raises(ModelValidationError):
            AcceptanceStep.from_dict({**_minimal_step(action), "value": "x"})
    if "locator" not in branch["properties"]:
        with pytest.raises(ModelValidationError):
            AcceptanceStep.from_dict({**_minimal_step(action), "locator": {"kind": "label", "value": "x"}})
    # And a field the branch requires is a field the constructor requires.
    for required in branch["required"]:
        if required == "action":
            continue
        with pytest.raises(ModelValidationError):
            AcceptanceStep.from_dict({k: v for k, v in _minimal_step(action).items() if k != required})


def test_role_locators_are_an_enum_and_other_kinds_are_text():
    role, other = locator_schema()["anyOf"]
    assert "button" in role["properties"]["value"]["enum"]
    assert role["properties"]["kind"]["enum"] == ["role"]
    assert "role" not in other["properties"]["kind"]["enum"]
    assert "label" in other["properties"]["kind"]["enum"]


def test_the_response_schema_is_closed_and_binary():
    schema = interviewer_response_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["status"]["enum"] == ["questions", "complete"]
    assert schema["properties"]["questions"]["maxItems"] == 3
    assert schema["properties"]["answers"]["properties"]["scenarios"]["items"]["properties"]["oracle"]["items"]["anyOf"]


# ── keys become ids; references are checked by name ─────────────────────────


def test_answers_from_keys_derives_ids_and_drops_empty_policies():
    answers = answers_from_keys({**_complete()["answers"], "roles": [], "data_policy": ["Kept until deleted."]})
    assert [r["id"] for r in answers["capabilities"]] == ["req.add-item"]
    assert [r["id"] for r in answers["quality_constraints"]] == ["req.keyboard"]
    assert [s["id"] for s in answers["scenarios"]] == ["scenario.add", "scenario.keyboard"]
    assert answers["scenarios"][0]["requirement_ids"] == ["req.add-item"]
    assert "roles" not in answers and answers["data_policy"] == ["Kept until deleted."]
    AdaptiveInterview(InterviewState("project.todo", "Todo", answers)).compile()


def test_an_unknown_requirement_key_is_named_in_the_error():
    candidate = _complete()["answers"]
    candidate["scenarios"][0]["requirement_keys"] = ["nope"]
    with pytest.raises(InterviewProposalError, match=r"'add'.*nope.*add-item"):
        answers_from_keys(candidate)


def test_duplicate_and_malformed_keys_are_rejected():
    candidate = _complete()["answers"]
    candidate["quality_constraints"][0]["key"] = "add-item"
    with pytest.raises(InterviewProposalError, match="declared twice"):
        answers_from_keys(candidate)
    candidate = _complete()["answers"]
    candidate["capabilities"][0]["key"] = "Add Item"
    with pytest.raises(InterviewProposalError, match="must match"):
        answers_from_keys(candidate)


# ── a turn ──────────────────────────────────────────────────────────────────


def test_a_complete_answer_compiles_first_try():
    outcome = _turn(_ScriptedProvider(_complete()))
    assert outcome.status == "complete" and outcome.attempts == 1 and outcome.source == "model"
    assert outcome.rejections == ()
    assert outcome.answers["scenarios"][0]["id"] == "scenario.add"
    assert outcome.usage.model_attempts == 1


def test_a_rejected_answer_is_retried_with_the_validators_own_message():
    broken = _complete()
    broken["answers"]["scenarios"][0]["requirement_keys"] = ["nope"]
    provider = _ScriptedProvider(broken, _complete())

    outcome = _turn(provider)

    assert outcome.status == "complete" and outcome.attempts == 2
    assert len(outcome.rejections) == 1 and "nope" in outcome.rejections[0]
    assert "Fix exactly this" in provider.requests[1].user_prompt
    assert "nope" in provider.requests[1].user_prompt


def test_questions_end_the_turn_without_validation():
    provider = _ScriptedProvider(
        {"status": "questions", "summary": "Two things first.",
         "questions": [{"prompt": "Who can see whose items?", "why": "Roles decide isolation."},
                       {"prompt": "How long are items kept?", "why": "Retention decides the data policy."}]}
    )
    outcome = _turn(provider)
    assert outcome.status == "questions" and outcome.attempts == 1
    assert [q["prompt"] for q in outcome.questions][0] == "Who can see whose items?"
    assert len(provider.requests) == 1


def test_garbage_never_raises():
    outcome = _turn(_ScriptedProvider("not an object"))
    assert outcome.status == "partial" and outcome.answers is None
    assert outcome.rejections == ("the answer was not a JSON object",) * 2


def test_a_candidate_that_still_does_not_compile_is_returned_as_partial():
    uncovered = _complete()
    uncovered["answers"]["scenarios"] = uncovered["answers"]["scenarios"][:1]  # keyboard has no scenario
    outcome = _turn(_ScriptedProvider(uncovered))
    assert outcome.status == "partial" and outcome.attempts == 2
    assert outcome.answers is not None and outcome.answers["capabilities"][0]["id"] == "req.add-item"
    assert all("req.keyboard" in rejection for rejection in outcome.rejections)


def test_the_prompt_states_the_rules_that_are_checked():
    system_prompt, user_prompt = interviewer_prompt(
        project_id="project.todo", project_name="Todo", transcript=_TRANSCRIPT, answers=None,
    )
    for action in AcceptanceAction:
        assert action.value in system_prompt
    assert "Never write ids" in system_prompt and "reload" in system_prompt
    assert "still there after a reload" in user_prompt


def test_form_fallback_asks_the_deterministic_questions_and_says_so():
    outcome = form_fallback(project_id="project.todo", project_name="Todo", answers=None)
    assert outcome.status == "questions" and outcome.source == "form-fallback"
    assert outcome.questions[0]["prompt"].startswith("What outcome")
    complete = form_fallback(
        project_id="project.todo", project_name="Todo", answers=answers_from_keys(_complete()["answers"])
    )
    assert complete.status == "complete" and complete.source == "form-fallback"


def test_model_interviewer_takes_a_fresh_gateway_per_turn():
    provider = _ScriptedProvider(_complete())
    interviewer = ModelInterviewer(
        gateway_factory=lambda: _gateway(provider), provider=PROVIDER, model="claude-sonnet-5",
    )
    outcome = interviewer.turn(project_id="project.todo", project_name="Todo", transcript=_TRANSCRIPT, answers=None)
    assert outcome.status == "complete"
    assert provider.requests[0].max_cost_usd == Decimal("0.50")
