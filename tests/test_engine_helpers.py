import json

import pytest

import build
import backend
import canvas
import codex_skill
import node


def test_project_parent_inputs_to_child_by_matching_name():
    child_contract = {
        "id": "latency_analyzer",
        "interface": {
            "operations": [
                {
                    "name": "analyze",
                    "inputs": {"events": "list"},
                    "outputs": {"latency": "dict"},
                    "errors": [],
                }
            ]
        },
    }
    parent_inputs = {
        "events": [{"endpoint": "/api", "latency_ms": 42}],
        "malformed_count": 0,
    }

    projected = build._project_parent_inputs_for_child(
        "assess_health", child_contract, parent_inputs)

    assert projected == {
        "assess_health": {
            "events": [{"endpoint": "/api", "latency_ms": 42}],
        }
    }


def test_project_parent_inputs_single_input_fallback():
    child_contract = {
        "id": "parse_events",
        "interface": {
            "operations": [
                {
                    "name": "parse",
                    "inputs": {"lines": "list"},
                    "outputs": {"events": "list"},
                    "errors": [],
                }
            ]
        },
    }
    parent_inputs = {"raw_lines": ["2026-01-01T00:00:00 INFO GET / 200 1"]}

    projected = build._project_parent_inputs_for_child(
        "log_health_report", child_contract, parent_inputs)

    assert projected == {"log_health_report": {"lines": parent_inputs["raw_lines"]}}


def test_prior_decision_source_defaults_to_live(tmp_path, monkeypatch):
    monkeypatch.setattr(build, "BUILD_ROOT", tmp_path)
    contract = {"id": "root"}

    assert build._prior_decision_source(contract) == "live"

    node_path = tmp_path / "root"
    node_path.mkdir()
    (node_path / "decision_source.txt").write_text("manual\n")

    assert build._prior_decision_source(contract) == "manual"


def test_codex_backend_selects_schema_by_skill_prompt():
    assert codex_skill._schema_for("You are a code generator")["required"] == ["source"]
    assert codex_skill._schema_for("You are a test generator")["required"] == ["tests"]
    assert codex_skill._schema_for("You are an architect who can decompose")["required"] == ["is_leaf"]


def test_codex_prompt_keeps_backend_text_only():
    prompt = codex_skill._prompt("system", "user")
    lower = prompt.lower()

    assert "do not inspect files" in lower
    assert "do not run commands" in lower
    assert "do not edit anything" in lower
    assert "json object" in lower


def test_backend_empty_env_uses_default(monkeypatch):
    monkeypatch.setenv("RICH_BACKEND", "  ")

    assert backend.selected(default="codex") == "codex"


def test_canned_pipeline_builds_in_temp_build_root(tmp_path, monkeypatch):
    import cli

    monkeypatch.setattr(build, "BUILD_ROOT", tmp_path)
    monkeypatch.setattr(node, "BUILD_ROOT", tmp_path)

    root = build.build(cli.ROOT_CONTRACT, allow_decompose=True, use_canned=True)
    main_path = build.assemble(root)

    assert root.id == "pipeline_demo"
    assert root.is_leaf is False
    assert {child.id for child in root.children} == {"normalizer", "validator"}
    assert main_path == str(tmp_path / "main.py")
    assert (tmp_path / "main.py").exists()


def test_canvas_vibe_marks_weather_fetcher_as_adapter():
    tree = {
        "id": "weather_reporter",
        "description": "fetch current weather for a city and make a report",
        "operations": [{"name": "run", "inputs": {"input": "string"},
                        "outputs": {"result": "string"}, "errors": []}],
        "is_leaf": False,
        "children": [
            {
                "id": "weather_fetcher",
                "description": "Fetches current weather data for a city from an external source",
                "operations": [{"name": "fetch", "inputs": {"city": "string"},
                                "outputs": {"weather_data": "dict"},
                                "errors": ["city_not_found", "fetch_failed"]}],
                "dependencies": [],
                "is_leaf": True,
                "children": [],
                "edges": [],
                "status": "planned",
            }
        ],
        "edges": [],
        "dependencies": [],
        "status": "planned",
    }

    tx = canvas.vibe_transaction(tree, "make this testable and split external IO out")
    canvas.apply_transaction(tree, tx)
    root = canvas.find_node(tree, "weather_reporter")
    fetcher = canvas.find_node(tree, "weather_fetcher")

    assert root["kind"] == "pure"
    assert fetcher["kind"] == "adapter"
    assert fetcher["external"]["provider"] == "Open-Meteo"
    assert fetcher["external"]["mock_policy"] == "unit_tests_mock_provider"


def test_canvas_adapter_contract_carries_external_boundary():
    cnode = {
        "id": "weather_fetcher",
        "description": "Fetches weather from Open-Meteo",
        "kind": "adapter",
        "external": {"provider": "Open-Meteo"},
        "operations": [{"name": "fetch", "inputs": {"city": "string"},
                        "outputs": {"weather_data": "dict"}, "errors": []}],
        "dependencies": [],
        "children": [],
        "edges": [],
        "status": "planned",
    }

    contract = canvas.node_to_contract(cnode)

    assert contract["module_kind"] == "adapter"
    assert contract["external"]["provider"] == "Open-Meteo"
    assert any(b["id"] == "external_boundary" for b in contract["behavior"])


def test_canvas_validation_flags_unmarked_external_adapter():
    tree = {
        "id": "weather_fetcher",
        "description": "Fetches current weather data from an external API",
        "operations": [{"name": "fetch", "inputs": {"city": "string"},
                        "outputs": {"weather_data": "dict"}, "errors": []}],
        "dependencies": [],
        "is_leaf": True,
        "children": [],
        "edges": [],
        "status": "planned",
    }

    cards = canvas.validation_cards(tree)

    assert any(c["action"] == "mark_adapter" for c in cards)


def test_canvas_project_round_trip(tmp_path, monkeypatch):
    project_path = tmp_path / "rich.canvas.json"
    monkeypatch.setattr(canvas, "PROJECT_PATH", project_path)
    tree = {
        "id": "root",
        "description": "demo",
        "operations": [{"name": "run", "inputs": {"input": "string"},
                        "outputs": {"result": "string"}, "errors": []}],
        "dependencies": [],
        "is_leaf": True,
        "children": [],
        "edges": [],
        "status": "planned",
    }

    canvas.save_project(tree, [{"transaction": {"summary": "saved"}}])
    loaded = canvas.load_project()

    assert loaded["tree"]["id"] == "root"
    assert loaded["tree"]["kind"] == "pure"
    assert loaded["history"][0]["transaction"]["summary"] == "saved"


def test_canvas_root_cause_flags_live_io_unit_tests(tmp_path, monkeypatch):
    monkeypatch.setattr(canvas, "BUILD_ROOT", tmp_path)
    node_dir = tmp_path / "weather_fetcher"
    (node_dir / "src").mkdir(parents=True)
    (node_dir / "tests").mkdir()
    (node_dir / "src" / "weather_fetcher.py").write_text(
        "import urllib.request\n"
        "def fetch(city):\n"
        "    return urllib.request.urlopen('https://example.test').read()\n"
    )
    (node_dir / "tests" / "test_weather_fetcher.py").write_text(
        "from weather_fetcher import fetch\n"
        "def test_fetch():\n"
        "    assert fetch('London')\n"
    )
    tree = {
        "id": "weather_fetcher",
        "description": "Fetch weather from an external API",
        "operations": [{"name": "fetch", "inputs": {"city": "string"},
                        "outputs": {"weather_data": "dict"}, "errors": []}],
        "dependencies": [],
        "is_leaf": True,
        "children": [],
        "edges": [],
        "status": "failed",
    }

    cards = canvas.inspect_external_io_risks(tree)

    assert any(c["title"] == "Unit tests may hit live external I/O" for c in cards)


def test_canvas_root_cause_ignores_stale_artifacts_for_planned_nodes(tmp_path, monkeypatch):
    monkeypatch.setattr(canvas, "BUILD_ROOT", tmp_path)
    node_dir = tmp_path / "weather_fetcher"
    (node_dir / "src").mkdir(parents=True)
    (node_dir / "tests").mkdir()
    (node_dir / "src" / "weather_fetcher.py").write_text(
        "import urllib.request\n"
        "def fetch(city):\n"
        "    return urllib.request.urlopen('https://example.test').read()\n"
    )
    (node_dir / "tests" / "test_weather_fetcher.py").write_text(
        "from weather_fetcher import fetch\n"
        "def test_fetch():\n"
        "    assert fetch('London')\n"
    )
    tree = {
        "id": "weather_fetcher",
        "description": "Fetch weather from an external API",
        "kind": "adapter",
        "external": {"provider": "Open-Meteo"},
        "operations": [{"name": "fetch", "inputs": {"city": "string"},
                        "outputs": {"weather_data": "dict"}, "errors": []}],
        "dependencies": [],
        "is_leaf": True,
        "children": [],
        "edges": [],
        "status": "planned",
    }

    cards = canvas.inspect_external_io_risks(tree)

    assert cards == []


def test_canvas_adapter_failed_live_io_card_does_not_suggest_marking_adapter(tmp_path, monkeypatch):
    monkeypatch.setattr(canvas, "BUILD_ROOT", tmp_path)
    node_dir = tmp_path / "weather_fetcher"
    (node_dir / "src").mkdir(parents=True)
    (node_dir / "tests").mkdir()
    (node_dir / "src" / "weather_fetcher.py").write_text(
        "import urllib.request\n"
        "def fetch(city):\n"
        "    return urllib.request.urlopen('https://example.test').read()\n"
    )
    (node_dir / "tests" / "test_weather_fetcher.py").write_text(
        "from weather_fetcher import fetch\n"
        "def test_fetch():\n"
        "    assert fetch('London')\n"
    )
    tree = {
        "id": "weather_fetcher",
        "description": "Fetch weather from an external API",
        "kind": "adapter",
        "external": {"provider": "Open-Meteo"},
        "operations": [{"name": "fetch", "inputs": {"city": "string"},
                        "outputs": {"weather_data": "dict"}, "errors": []}],
        "dependencies": [],
        "is_leaf": True,
        "children": [],
        "edges": [],
        "status": "failed",
    }

    cards = canvas.inspect_external_io_risks(tree)

    assert any(c["title"] == "Adapter tests still hit live I/O" and c["action"] is None
               for c in cards)


def test_canvas_normalize_internal_adapter_back_to_pure():
    tree = {
        "id": "weather_reporter",
        "description": "Coordinate weather report generation",
        "kind": "adapter",
        "external": {"provider": "Open-Meteo"},
        "operations": [{"name": "run", "inputs": {"input": "string"},
                        "outputs": {"result": "string"}, "errors": []}],
        "dependencies": [],
        "is_leaf": False,
        "children": [
            {
                "id": "weather_fetcher",
                "description": "Fetch weather from an external API",
                "kind": "adapter",
                "external": {"provider": "Open-Meteo"},
                "operations": [{"name": "fetch", "inputs": {"city": "string"},
                                "outputs": {"weather_data": "dict"}, "errors": []}],
                "dependencies": [],
                "is_leaf": True,
                "children": [],
                "edges": [],
                "status": "planned",
            }
        ],
        "edges": [],
        "status": "planned",
    }

    canvas.normalize_canvas_node(tree)

    assert tree["kind"] == "pure"
    assert tree["external"] == {}
    assert tree["children"][0]["kind"] == "adapter"


def test_canvas_transaction_creates_child_and_dataflow_edge():
    tree = {
        "id": "reporter",
        "description": "make reports",
        "operations": [{"name": "run", "inputs": {"input": "string"},
                        "outputs": {"result": "string"}, "errors": []}],
        "dependencies": [],
        "is_leaf": True,
        "children": [],
        "edges": [],
        "status": "planned",
    }

    canvas.apply_transaction(tree, {
        "ops": [
            {
                "op": "create_child",
                "parent_id": "reporter",
                "node": {
                    "id": "loader",
                    "description": "load prepared data",
                    "operations": [{"name": "load", "inputs": {"query": "string"},
                                    "outputs": {"record": "dict"}, "errors": []}],
                },
            },
            {
                "op": "create_child",
                "parent_id": "reporter",
                "node": {
                    "id": "formatter",
                    "description": "format data",
                    "operations": [{"name": "format", "inputs": {"record": "dict"},
                                    "outputs": {"result": "string"}, "errors": []}],
                },
            },
            {"op": "add_edge", "parent_id": "reporter", "from": "loader",
             "to": "formatter", "name": "record"},
        ]
    })

    assert tree["is_leaf"] is False
    assert {c["id"] for c in tree["children"]} == {"loader", "formatter"}
    assert tree["edges"] == [{"from": "loader", "to": "formatter", "name": "record"}]
    assert not [c for c in canvas.validation_cards(tree) if c["severity"] == "error"]


def test_canvas_transaction_rename_updates_edges_and_dependencies():
    tree = {
        "id": "weather_reporter",
        "description": "report weather",
        "operations": [{"name": "run", "inputs": {"input": "string"},
                        "outputs": {"result": "string"}, "errors": []}],
        "dependencies": [],
        "is_leaf": False,
        "children": [
            {
                "id": "weather_fetcher",
                "description": "fetch weather",
                "operations": [{"name": "fetch", "inputs": {"city": "string"},
                                "outputs": {"weather": "dict"}, "errors": []}],
                "dependencies": [],
                "is_leaf": True,
                "children": [],
                "edges": [],
                "status": "planned",
            },
            {
                "id": "formatter",
                "description": "format weather",
                "operations": [{"name": "format", "inputs": {"weather": "dict"},
                                "outputs": {"result": "string"}, "errors": []}],
                "dependencies": [{"name": "weather_fetcher", "id": "weather_fetcher"}],
                "is_leaf": True,
                "children": [],
                "edges": [],
                "status": "planned",
            },
        ],
        "edges": [{"from": "weather_fetcher", "to": "formatter", "name": "weather"}],
        "status": "planned",
    }

    canvas.apply_transaction(tree, {
        "ops": [{"op": "rename_node", "node_id": "weather_fetcher",
                 "new_id": "weather_provider"}]
    })

    assert canvas.find_node(tree, "weather_fetcher") is None
    assert canvas.find_node(tree, "weather_provider") is not None
    assert tree["edges"] == [{"from": "weather_provider", "to": "formatter", "name": "weather"}]
    formatter = canvas.find_node(tree, "formatter")
    assert formatter["dependencies"] == [{"name": "weather_provider", "id": "weather_provider"}]
    assert not [c for c in canvas.validation_cards(tree) if c["severity"] == "error"]


def test_canvas_vibe_add_cache_creates_stateful_boundary(monkeypatch):
    monkeypatch.delenv("RICH_CANVAS_LLM_VIBE", raising=False)
    tree = {
        "id": "weather_reporter",
        "description": "fetch current weather for a city and make a report",
        "operations": [{"name": "run", "inputs": {"input": "string"},
                        "outputs": {"result": "string"}, "errors": []}],
        "dependencies": [],
        "is_leaf": False,
        "children": [
            {
                "id": "weather_fetcher",
                "description": "Fetches current weather data for a city",
                "operations": [{"name": "fetch", "inputs": {"city": "string"},
                                "outputs": {"weather_data": "dict"}, "errors": []}],
                "dependencies": [],
                "is_leaf": True,
                "children": [],
                "edges": [],
                "status": "planned",
            }
        ],
        "edges": [],
        "status": "planned",
    }

    tx = canvas.transaction_for_prompt(tree, "add caching", "weather_fetcher")
    canvas.apply_transaction(tree, tx)

    cache = canvas.find_node(tree, "weather_fetcher_cache")
    fetcher = canvas.find_node(tree, "weather_fetcher")

    assert tx["source"] == "deterministic"
    assert cache["kind"] == "stateful"
    assert {op["name"] for op in cache["operations"]} == {"lookup", "remember"}
    assert fetcher["dependencies"] == [{"name": "cache", "id": "weather_fetcher_cache"}]


def test_canvas_architecture_proposal_isolates_weather_adapter():
    proposal = canvas.architecture_proposal({
        "goal": "Build a weather app that fetches current weather for a city and prints a short report.",
        "root_id": "weather_reporter",
        "external_services": "Open-Meteo",
        "state": "stateless",
        "rigor": "strict tests",
    })
    tree = proposal["preview_tree"]

    assert tree["id"] == "weather_reporter"
    assert tree["is_leaf"] is False
    assert canvas.find_node(tree, "weather_api_adapter")["kind"] == "adapter"
    assert canvas.find_node(tree, "weather_reporter")["kind"] == "pure"
    assert canvas.find_node(tree, "weather_fetcher")["dependencies"] == [
        {"name": "weather_api", "id": "weather_api_adapter"}
    ]
    assert proposal["transaction"]["ops"][0]["op"] == "replace_tree"
    assert not [c for c in proposal["cards"] if c["severity"] == "error"]
    assert any(item["title"] == "Unit tests fake providers" and item["ok"]
               for item in proposal["checklist"])


def test_canvas_architecture_proposal_adds_cache_when_requested():
    proposal = canvas.architecture_proposal({
        "goal": "Build a production weather app with caching.",
        "root_id": "weather_reporter",
        "external_services": "Open-Meteo",
        "state": "cached",
    })
    tree = proposal["preview_tree"]

    cache = canvas.find_node(tree, "weather_cache")
    fetcher = canvas.find_node(tree, "weather_fetcher")

    assert cache["kind"] == "stateful"
    assert {"name": "cache", "id": "weather_cache"} in fetcher["dependencies"]
    assert any(item["title"] == "State is intentional" and item["ok"]
               for item in proposal["checklist"])


def test_canvas_replace_tree_transaction_swaps_entire_canvas():
    tree = {
        "id": "old_root",
        "description": "old",
        "operations": [{"name": "run", "inputs": {"input": "string"},
                        "outputs": {"result": "string"}, "errors": []}],
        "dependencies": [],
        "is_leaf": True,
        "children": [],
        "edges": [],
        "status": "planned",
    }
    next_tree = {
        "id": "new_root",
        "description": "new",
        "operations": [{"name": "run", "inputs": {"input": "string"},
                        "outputs": {"result": "string"}, "errors": []}],
        "dependencies": [],
        "is_leaf": True,
        "children": [],
        "edges": [],
        "status": "planned",
    }

    updated = canvas.apply_transaction(tree, {"ops": [{"op": "replace_tree", "tree": next_tree}]})

    assert updated["id"] == "new_root"
    assert updated["description"] == "new"
    assert updated is not tree
    assert canvas.find_node(updated, "old_root") is None


def test_canvas_suggest_root_brief_uses_deterministic_fallback(monkeypatch):
    monkeypatch.setattr(canvas, "llm_brief_suggestion", lambda *args, **kwargs: None)

    suggestion = canvas.suggest_root_brief({"goal": "Build a weather app"}, None)

    assert suggestion["source"] == "deterministic"
    assert suggestion["suggestion"]["root_id"] == "weather_reporter"
    assert suggestion["preview"]["id"] == "weather_reporter"
    assert suggestion["preview"]["description"] == "Build a weather app"
    assert suggestion["preview"]["external_services"] == "Open-Meteo"
    assert suggestion["preview"]["inputs"] == "city: London"
    assert suggestion["preview"]["outputs"] == "report: concise weather summary"
    assert suggestion["preview"]["state"] == "cached"
    assert suggestion["preview"]["shape"] == "production"
    assert suggestion["rationale"]


def test_canvas_architecture_proposal_handles_empty_brief():
    proposal = canvas.architecture_proposal({})
    tree = proposal["preview_tree"]

    assert tree["id"] == "app"
    assert tree["description"] == "Build a small software system."
    assert proposal["transaction"]["ops"][0]["op"] == "replace_tree"


# ── build-loop: per-node test report + scoped rebuild ──────────────

def test_save_test_report_persists_failure_detail(tmp_path, monkeypatch):
    monkeypatch.setattr(node, "BUILD_ROOT", tmp_path)
    n = node.Node(id="m", contract={"id": "m"}, is_leaf=True)

    build._save_test_report(n, {"passed": False,
                                "failures": ["FAILED test_x - assert 1 == 2"]}, 3)

    report = json.loads((tmp_path / "m" / "test_report.json").read_text())
    assert report["passed"] is False
    assert report["attempts"] == 3
    assert any("FAILED" in f for f in report["failures"])


def test_invalidate_node_drops_memo_and_resets_status(tmp_path, monkeypatch):
    monkeypatch.setattr(build, "BUILD_ROOT", tmp_path)
    node_dir = tmp_path / "m"
    node_dir.mkdir()
    (node_dir / "memo.txt").write_text("deadbeef")
    (node_dir / "status.json").write_text(json.dumps({"status": "verified",
                                                       "reason": "stale"}))

    build.invalidate_node("m")

    assert not (node_dir / "memo.txt").exists()
    status = json.loads((node_dir / "status.json").read_text())
    assert status["status"] == "planned"
    assert "reason" not in status


def test_invalidate_then_rebuild_only_rebuilds_target(tmp_path, monkeypatch):
    import cli

    monkeypatch.setattr(build, "BUILD_ROOT", tmp_path)
    monkeypatch.setattr(node, "BUILD_ROOT", tmp_path)

    build.build(cli.FAN_IN_ROOT_CONTRACT, use_canned=True)        # full build
    build.invalidate_node("format_checker")
    (tmp_path / "manifest.jsonl").write_text("")                  # observe the rebuild only
    build.build(cli.FAN_IN_ROOT_CONTRACT, use_canned=True)

    events = [
        json.loads(line)
        for line in (tmp_path / "manifest.jsonl").read_text().splitlines()
        if line.strip()
    ]
    memo_hits = {e["node"] for e in events if e["event"] == "memo-hit"}

    assert "format_checker" not in memo_hits      # invalidated → rebuilt
    assert "regex_engine" in memo_hits            # sibling leaf reused
    assert "domain_checker" in memo_hits          # sibling leaf reused


def test_read_node_artifacts_returns_src_tests_report(tmp_path, monkeypatch):
    monkeypatch.setattr(canvas, "BUILD_ROOT", tmp_path)
    node_dir = tmp_path / "m"
    (node_dir / "src").mkdir(parents=True)
    (node_dir / "tests").mkdir()
    (node_dir / "status.json").write_text(json.dumps({"status": "verified"}))
    (node_dir / "test_report.json").write_text(
        json.dumps({"passed": True, "failures": [], "attempts": 1}))
    (node_dir / "src" / "m.py").write_text("def run():\n    return 1\n")
    (node_dir / "tests" / "test_m.py").write_text("def test_run():\n    assert True\n")

    art = canvas.read_node_artifacts("m")

    assert art["status"] == "verified" and art["built"] is True
    assert art["report"]["passed"] is True
    assert "m.py" in art["src"]
    assert "test_m.py" in art["tests"]


def test_read_node_artifacts_missing_node_is_empty():
    art = canvas.read_node_artifacts("nope_does_not_exist_zzz")

    assert art["built"] is False
    assert art["status"] == "unplanned"
    assert art["report"] is None
    assert art["src"] == {} and art["tests"] == {}


# ── fail-closed semantic composition evidence ─────────────────────

def _real_composition_fixture(tmp_path, monkeypatch, child_source):
    monkeypatch.setattr(build, "BUILD_ROOT", tmp_path)
    monkeypatch.setattr(node, "BUILD_ROOT", tmp_path)

    child_contract = {
        "id": "real_child",
        "interface": {
            "operations": [{
                "name": "run",
                "inputs": {"text": "string"},
                "outputs": {"value": "string"},
                "errors": [],
            }]
        },
    }
    root_contract = {
        "id": "semantic_root",
        "interface": {
            "operations": [{
                "name": "run",
                "inputs": {"text": "string"},
                "outputs": {"value": "string"},
                "errors": [],
            }]
        },
    }
    child = node.Node(id="real_child", contract=child_contract, is_leaf=True)
    root = node.Node(
        id="semantic_root",
        contract=root_contract,
        is_leaf=False,
        children=[child],
    )

    child.src_path().mkdir(parents=True)
    child.src_path().joinpath("real_child.py").write_text(child_source)
    root.src_path().mkdir(parents=True)
    root.src_path().joinpath("semantic_root.py").write_text(
        "class SemanticRoot:\n"
        "    def __init__(self, real_child):\n"
        "        self.real_child = real_child\n"
        "    def run(self, text):\n"
        "        return self.real_child.run(text=text)\n"
    )
    root.tests_path().mkdir()
    root.tests_path().joinpath("test_semantic_root.py").write_text(
        "from semantic_root import SemanticRoot\n"
        "\n"
        "class FakeChild:\n"
        "    def run(self, text):\n"
        "        return {'value': 'todo:' + text}\n"
        "\n"
        "def test_required_behavior():\n"
        "    app = SemanticRoot(FakeChild())\n"
        "    assert app.run(text='milk') == {'value': 'todo:milk'}\n"
    )
    return root


def test_real_composition_rejects_fake_real_semantic_split(tmp_path, monkeypatch):
    root = _real_composition_fixture(
        tmp_path,
        monkeypatch,
        "def run(text):\n"
        "    return {'value': text.upper()}\n",
    )

    # The old verifier stopped here: the parent passes against its semantically
    # stronger fake even though the real child does something else.
    assert build.run_tests(root.src_path(), root.tests_path())["passed"] is True

    evidence = build.verify_real_composition(root)

    assert evidence["passed"] is False
    assert any("assert" in failure or "FAILED" in failure
               for failure in evidence["failures"])


def test_real_composition_accepts_matching_real_child(tmp_path, monkeypatch):
    root = _real_composition_fixture(
        tmp_path,
        monkeypatch,
        "def run(text):\n"
        "    return {'value': 'todo:' + text}\n",
    )

    evidence = build.verify_real_composition(root)

    assert evidence == {"passed": True, "failures": []}


def test_run_tests_fails_when_verification_suite_is_empty(tmp_path):
    src_dir = tmp_path / "src"
    tests_dir = tmp_path / "tests"
    src_dir.mkdir()
    tests_dir.mkdir()

    result = build.run_tests(src_dir, tests_dir)

    assert result["passed"] is False
    assert "No tests found" in result["failures"][0]


def test_internal_memo_without_real_composition_evidence_is_not_reused(
        tmp_path, monkeypatch):
    monkeypatch.setattr(build, "BUILD_ROOT", tmp_path)
    monkeypatch.setattr(node, "BUILD_ROOT", tmp_path)

    contract = {"id": "legacy_internal", "interface": {"operations": []}}
    child = node.Node(
        id="legacy_child",
        contract={"id": "legacy_child", "interface": {"operations": []}},
        is_leaf=True,
    )
    legacy = node.Node(
        id="legacy_internal",
        contract=contract,
        is_leaf=False,
        children=[child],
    )
    node.save_contract(legacy)
    node.save_decision(legacy)
    node.save_status(legacy, "verified")
    build._save_memo(legacy, contract)
    build._save_test_report(
        legacy,
        {"passed": True, "failures": []},
        attempts=1,
    )

    assert build._load_verified_node(contract) is None


def test_build_refuses_to_verify_internal_node_from_fake_only_tests(
        tmp_path, monkeypatch):
    monkeypatch.setattr(build, "BUILD_ROOT", tmp_path)
    monkeypatch.setattr(node, "BUILD_ROOT", tmp_path)
    monkeypatch.setattr(build, "K_WIRE", 1)

    child_contract = {
        "id": "real_child",
        "interface": {
            "operations": [{
                "name": "run",
                "inputs": {"text": "string"},
                "outputs": {"value": "string"},
                "errors": [],
            }]
        },
    }
    root_contract = {
        "id": "semantic_root",
        "interface": {
            "operations": [{
                "name": "run",
                "inputs": {"text": "string"},
                "outputs": {"value": "string"},
                "errors": [],
            }]
        },
    }

    def fake_plan(contract, allow_decompose=False):
        if contract["id"] == "semantic_root":
            return {"is_leaf": False, "children": [child_contract], "edges": []}
        return {"is_leaf": True}

    def fake_tests(contract, **kwargs):
        if contract["id"] == "real_child":
            return (
                "from real_child import run\n"
                "def test_child_generic_behavior():\n"
                "    assert run(text='milk') == {'value': 'MILK'}\n"
            )
        return (
            "from semantic_root import SemanticRoot\n"
            "class FakeChild:\n"
            "    def run(self, text):\n"
            "        return {'value': 'todo:' + text}\n"
            "def test_required_behavior():\n"
            "    assert SemanticRoot(FakeChild()).run(text='milk') == "
            "{'value': 'todo:milk'}\n"
        )

    def fake_implement(contract, **kwargs):
        if contract["id"] == "real_child":
            return "def run(text):\n    return {'value': text.upper()}\n"
        return (
            "class SemanticRoot:\n"
            "    def __init__(self, real_child):\n"
            "        self.real_child = real_child\n"
            "    def run(self, text):\n"
            "        return self.real_child.run(text=text)\n"
        )

    monkeypatch.setattr(build, "plan", fake_plan)
    monkeypatch.setattr(build, "derive_tests", fake_tests)
    monkeypatch.setattr(build, "implement", fake_implement)

    with pytest.raises(build.BuildFailure, match="wiring failed"):
        build.build(root_contract, allow_decompose=True)

    status = json.loads(
        (tmp_path / "semantic_root" / "status.json").read_text())
    report = json.loads(
        (tmp_path / "semantic_root" / "test_report.json").read_text())
    assert status["status"] == "failed"
    assert report["passed"] is False
    assert report["evidence"]["unit"]["passed"] is True
    assert report["evidence"]["real_composition"]["passed"] is False
    assert not (tmp_path / "semantic_root" / "memo.txt").exists()


def test_shared_state_integration_unavailable_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(build, "BUILD_ROOT", tmp_path)
    monkeypatch.setattr(node, "BUILD_ROOT", tmp_path)

    store = {
        "id": "store",
        "stateful": True,
        "interface": {"operations": []},
    }
    writer_a = {
        "id": "writer_a",
        "dependencies": [{"name": "store", "id": "store"}],
        "interface": {"operations": []},
    }
    writer_b = {
        "id": "writer_b",
        "dependencies": [{"name": "store", "id": "store"}],
        "interface": {"operations": []},
    }
    root_contract = {
        "id": "shared_root",
        "interface": {"operations": []},
    }
    root_decision = {
        "is_leaf": False,
        "children": [store, writer_a, writer_b],
        "edges": [
            {"from": "store", "to": "writer_a", "name": "store"},
            {"from": "store", "to": "writer_b", "name": "store"},
        ],
    }

    monkeypatch.setattr(
        build,
        "plan",
        lambda contract, allow_decompose=False:
            root_decision if contract["id"] == "shared_root" else {"is_leaf": True},
    )
    monkeypatch.setattr(
        build,
        "derive_tests",
        lambda contract, **kwargs: "def test_placeholder():\n    assert True\n",
    )
    monkeypatch.setattr(
        build,
        "implement",
        lambda contract, **kwargs: "def placeholder():\n    return None\n",
    )
    monkeypatch.setattr(
        build,
        "run_tests",
        lambda src_dir, tests_dir: {"passed": True, "failures": []},
    )
    monkeypatch.setattr(
        build,
        "verify_real_composition",
        lambda current: {"passed": True, "failures": []},
    )

    def unavailable(_contract):
        raise build.LLMNotConfigured("integration model is offline")

    monkeypatch.setattr(build, "derive_integration_test", unavailable)

    with pytest.raises(build.BuildFailure, match="integration test FAILED"):
        build.build(root_contract, allow_decompose=True)

    report = json.loads(
        (tmp_path / "shared_root" / "test_report.json").read_text())
    assert report["passed"] is False
    shared_evidence = report["evidence"]["shared_stateful_integration"]
    assert shared_evidence["passed"] is False
    assert "unavailable" in shared_evidence["failures"][0]
