"""One prose paragraph, one real model, a specification that compiles.

The offline suite proves the interviewer's discipline with a scripted provider.
This proves the thing that discipline exists for: that a person's own words,
on the claude-code route, become requirements, scenarios and an executable
oracle the compiler accepts -- with no human edits in between.
"""

from decimal import Decimal

import pytest

from liveutil import require_claude_login
from richbuild.interview import AdaptiveInterview, InterviewState
from richbuild.runtime import CLAUDE_CODE_ROUTE, default_interviewer

PROSE = (
    "A reading list for one person. I add a book by its title and author, see "
    "every book I have added in a list, and mark a book as finished so it moves "
    "to a finished section. It has to still be there after I reload the page, "
    "and I want to be able to do all of it with only a keyboard."
)


@pytest.mark.live
def test_one_paragraph_becomes_a_specification_that_compiles():
    require_claude_login()
    interviewer = default_interviewer(route=CLAUDE_CODE_ROUTE, max_cost_usd=Decimal("1.00"))
    assert interviewer is not None, "the claude-code route did not construct"

    transcript = [{"role": "user", "text": PROSE}]
    outcome = interviewer.turn(
        project_id="project.reading-list",
        project_name="Reading list",
        transcript=transcript,
        answers=None,
    )
    if outcome.status == "questions":
        # One round of answering is allowed; the questions must be about
        # something the paragraph genuinely left open.
        reply = "Only I use it, on my own laptop. Keep books forever unless I delete them."
        transcript += [
            {"role": "interviewer", "text": outcome.summary},
            {"role": "user", "text": reply},
        ]
        outcome = interviewer.turn(
            project_id="project.reading-list",
            project_name="Reading list",
            transcript=transcript,
            answers=outcome.answers,
        )

    assert outcome.status == "complete", (outcome.status, outcome.rejections)
    assert outcome.source == "model"
    answers = outcome.answers
    assert answers is not None

    spec = AdaptiveInterview(
        InterviewState("project.reading-list", "Reading list", answers)
    ).compile()
    assert len(spec.requirements) >= 2
    assert spec.acceptance_scenarios
    actions = {
        step.action.value
        for scenario in spec.acceptance_scenarios
        for step in scenario.oracle
    }
    # A persistence scenario reloads; every scenario asserts something.
    assert "reload" in actions
    assert any(action.startswith("assert_") for action in actions)
    for scenario in spec.acceptance_scenarios:
        assert scenario.oracle[-1].action.value.startswith("assert_"), scenario.id
    assert outcome.usage.cost_usd > Decimal("0")
