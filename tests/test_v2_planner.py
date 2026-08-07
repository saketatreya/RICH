from rich_v2.compiler import compile_architecture
from rich_v2.interview import AdaptiveInterview, InterviewState
from rich_v2.models import EdgeKind
from rich_v2.planner import plan_nextjs_architecture


def _project(*, external=False):
    statement = "A member can add a task and it remains after refresh."
    if external:
        statement += " Send an email through an external API."
    answers = {
        "goal": "A persistent team todo application",
        "audiences": ["small product teams"],
        "roles": ["Members can manage tasks only in their own team."],
        "capabilities": [
            {
                "id": "req.todo",
                "title": "Manage tasks",
                "statement": statement,
            }
        ],
        "data_policy": ["Tasks remain until explicitly deleted."],
        "quality_constraints": [
            {
                "id": "req.a11y",
                "title": "Accessible UI",
                "statement": "Task actions satisfy keyboard accessibility.",
            }
        ],
        "scenarios": [
            {
                "id": "scenario.todo",
                "title": "Add task",
                "when": ["A member adds Buy milk."],
                "then": ["Buy milk remains after refresh."],
                "requirement_ids": ["req.todo"],
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
                "title": "Keyboard access",
                "when": ["A member uses only a keyboard."],
                "then": ["They can add and complete a task."],
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
    if external:
        answers["integration_failure_policy"] = [
            "Queue email during provider outage and show pending state."
        ]
    return AdaptiveInterview(
        InterviewState("project.todo", "Todo", answers=answers)
    ).compile()


def test_web_plan_is_valid_and_keeps_approved_semantics_in_contracts():
    project = _project()

    proposal = plan_nextjs_architecture(project)
    architecture = proposal.architecture

    architecture.validate_against_project(project)
    assert architecture.target_pack == "nextjs-app-router"
    assert {"app", "web", "domain", "data", "postgres"} <= set(
        architecture.node_index
    )
    assert set(project.requirement_index) <= {
        requirement_id
        for contract in architecture.contracts
        for requirement_id in contract.traced_requirement_ids
    }
    serialized_contracts = str([contract.to_dict() for contract in architecture.contracts])
    assert "A member can add a task" in serialized_contracts
    assert "Buy milk remains after refresh" in serialized_contracts
    compiled = compile_architecture(architecture, project)
    assert "domain" in compiled.task_index["web"].dependency_ids
    assert "postgres" not in compiled.task_index


def test_external_requirement_adds_adapter_boundary_and_failure_policy():
    proposal = plan_nextjs_architecture(_project(external=True))
    architecture = proposal.architecture

    assert "adapters" in architecture.node_index
    assert any(
        edge.kind is EdgeKind.CAPABILITY and edge.target_node_id == "adapters"
        for edge in architecture.edges
    )
    assert not proposal.risks


def test_same_project_produces_identical_architecture_document():
    project = _project()

    assert (
        plan_nextjs_architecture(project).architecture.to_dict()
        == plan_nextjs_architecture(project).architecture.to_dict()
    )
