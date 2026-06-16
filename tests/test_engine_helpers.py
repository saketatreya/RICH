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
