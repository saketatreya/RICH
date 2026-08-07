import json

from rich_v2.control_plane import ControlPlane
from rich_v2.store import RichStore


def _approved_scaffold(tmp_path, *, project_id, name, answers):
    control_plane = ControlPlane(RichStore(tmp_path / f"state-{project_id}"))
    project = control_plane.create_project(project_id=project_id, name=name)
    spec = control_plane.submit_interview(
        project_id=project["id"],
        project_name=project["name"],
        answers=answers,
        expected_revision=0,
    )
    control_plane.decide_approval(
        spec.approval["id"], approved=True, actor="product-owner"
    )
    architecture = control_plane.propose_architecture(
        project_id=project["id"],
        spec_revision_id=spec.revision.id,
        spec_approval_id=spec.approval["id"],
        expected_revision=1,
    )
    control_plane.decide_approval(
        architecture.approval["id"], approved=True, actor="product-owner"
    )
    run = control_plane.prepare_run(
        architecture_approval_id=architecture.approval["id"],
        budget={
            "max_model_attempts": 5,
            "max_input_tokens": 80_000,
            "max_output_tokens": 40_000,
            "max_cost_usd": "10",
            "max_execution_seconds": 600,
        },
    )
    destination = tmp_path / f"generated-{project_id}"
    scaffold = control_plane.scaffold_run(
        run_id=run.run["id"],
        destination=destination,
    )
    return destination, scaffold


def _answers(
    *,
    goal,
    requirement_id,
    title,
    statement,
    scenario_id,
    scenario_title,
    adaptive_key,
    adaptive_value,
):
    return {
        "goal": goal,
        "audiences": ["operations teams"],
        "capabilities": [
            {
                "id": requirement_id,
                "title": title,
                "statement": statement,
            }
        ],
        "quality_constraints": [
            {
                "id": f"{requirement_id}.a11y",
                "title": "Keyboard access",
                "statement": f"{title} is operable with a keyboard.",
            }
        ],
        adaptive_key: [adaptive_value],
        "scenarios": [
            {
                "id": scenario_id,
                "title": scenario_title,
                "when": [f"An operator chooses {title}."],
                "then": [statement],
                "requirement_ids": [requirement_id],
                "oracle": [
                    {"action": "open_requirement"},
                    {
                        "action": "assert_visible",
                        "locator": {"kind": "text", "value": statement},
                    },
                ],
            },
            {
                "id": f"{scenario_id}.a11y",
                "title": f"Keyboard {title}",
                "when": ["An operator uses only a keyboard."],
                "then": [f"They can complete {title}."],
                "requirement_ids": [f"{requirement_id}.a11y"],
                "oracle": [
                    {"action": "navigate", "value": "/"},
                    {"action": "keyboard", "value": "Tab"},
                    {
                        "action": "assert_visible",
                        "locator": {"kind": "role", "value": "link"},
                    },
                ],
            },
        ],
    }


def test_approved_specs_and_architectures_generate_materially_different_software(
    tmp_path,
):
    inventory, inventory_scaffold = _approved_scaffold(
        tmp_path,
        project_id="inventory",
        name="Inventory Ledger",
        answers=_answers(
            goal="Persist inventory records in a database across restarts.",
            requirement_id="req.inventory",
            title="Reconcile inventory",
            statement="A warehouse operator stores an audited stock adjustment.",
            scenario_id="scenario.inventory",
            scenario_title="Stock adjustment persists",
            adaptive_key="data_policy",
            adaptive_value="Inventory records remain until an administrator removes them.",
        ),
    )
    notifications, notification_scaffold = _approved_scaffold(
        tmp_path,
        project_id="notifications",
        name="Notification Relay",
        answers=_answers(
            goal="Deliver email alerts through an external API.",
            requirement_id="req.notification",
            title="Send alert",
            statement="An operator sends a delivery alert through the email provider.",
            scenario_id="scenario.notification",
            scenario_title="Delivery alert is sent",
            adaptive_key="integration_failure_policy",
            adaptive_value="A provider outage returns a retryable failure.",
        ),
    )

    inventory_intent = json.loads(
        (inventory / ".rich/product-intent.json").read_text()
    )
    notification_intent = json.loads(
        (notifications / ".rich/product-intent.json").read_text()
    )
    assert inventory_intent["project_spec"]["requirements"][0]["id"] == "req.inventory"
    assert (
        notification_intent["project_spec"]["requirements"][0]["id"]
        == "req.notification"
    )
    assert inventory_intent != notification_intent

    inventory_routes = {
        path.parent.name
        for path in (inventory / "apps/web/src/app/api/capabilities").glob(
            "*/route.ts"
        )
    }
    notification_routes = {
        path.parent.name
        for path in (notifications / "apps/web/src/app/api/capabilities").glob(
            "*/route.ts"
        )
    }
    assert inventory_routes
    assert notification_routes
    assert inventory_routes.isdisjoint(notification_routes)

    inventory_domain = (
        inventory / "packages/domain/src/product-intent.ts"
    ).read_text()
    notification_domain = (
        notifications / "packages/domain/src/product-intent.ts"
    ).read_text()
    notification_contracts = (
        notifications / "packages/contracts/src/product-intent.ts"
    ).read_text()
    assert "req.inventory" in inventory_domain
    assert "req.notification" in notification_domain
    assert inventory_domain != notification_domain
    assert "req.notification" in notification_contracts
    assert "operation:domain:req_notification" in notification_contracts

    inventory_scenarios = {
        path.name for path in (inventory / "tests/e2e/scenarios").glob("*.spec.ts")
    }
    notification_scenarios = {
        path.name
        for path in (notifications / "tests/e2e/scenarios").glob("*.spec.ts")
    }
    assert inventory_scenarios
    assert notification_scenarios
    assert inventory_scenarios.isdisjoint(notification_scenarios)
    assert "Stock adjustment persists" in " ".join(
        path.read_text()
        for path in (inventory / "tests/e2e/scenarios").glob("*.spec.ts")
    )
    assert "Delivery alert is sent" in " ".join(
        path.read_text()
        for path in (notifications / "tests/e2e/scenarios").glob("*.spec.ts")
    )

    assert (inventory / "packages/db").is_dir()
    assert not (inventory / "packages/adapters").exists()
    assert not (notifications / "packages/db").exists()
    assert (notifications / "packages/adapters").is_dir()
    assert inventory_scaffold.manifest.content_digest != (
        notification_scaffold.manifest.content_digest
    )
