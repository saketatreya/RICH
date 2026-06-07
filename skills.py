"""LLM skills — PLAN, IMPLEMENT, DERIVE_TESTS.

M-A/M-B: All skills return hardcoded (canned) results.
M-C onward: IMPLEMENT and DERIVE_TESTS become real LLM calls.
PLAN stays stubbed (only is_leaf:true) until M-D.
"""

import os
import json
import yaml
from llm import (
    call_with_retry,
    parse_json_response,
    is_available,
    LLMNotConfigured,
    LLMParseError,
)


# ── Phase 9 (§2.4): selective per-skill model routing ──────────────
# PLAN (the bet) and IMPLEMENT (the code) stay on the strong default model — these are
# the thing under test and must NOT be cheaped out. DERIVE_TESTS / integration-test
# generation MAY be routed to a cheaper model via env to save quota on a long live build.
# Default is None → the backend's default model (sonnet) → behavior unchanged unless an
# operator opts in. model=None flows through call_with_retry to the backend default.
PLAN_MODEL = os.environ.get("RICH_PLAN_MODEL") or None
IMPL_MODEL = os.environ.get("RICH_IMPL_MODEL") or None
TESTS_MODEL = os.environ.get("RICH_TESTS_MODEL") or None


# ── PLAN (real LLM from M-D, decomposition from M-E) ───────────────

PLAN_SYSTEM_LEAF_ONLY = """You are an architect for a recursive agent build system called RICH.
Your job: given a module CONTRACT, decide if it can be implemented directly as a leaf module.

CRITICAL RESTRICTION: You may ONLY return {"is_leaf": true}. Decomposition is disabled.
Do NOT return children or edges under any circumstances. If you think the module
should be decomposed, return {"is_leaf": true} anyway — this is a leaf-only mode.

Output format: a JSON object with a single key "is_leaf" set to true.
Example: {"is_leaf": true}"""

PLAN_SYSTEM_DECOMPOSE = """You are an architect for a recursive agent build system called RICH.
Your job: given a ROOT CONTRACT, decompose it into child modules if appropriate,
or decide it's simple enough to implement directly as a leaf.

DECISION RULES:
- Base the leaf-vs-decompose decision on the BEHAVIOR, not on how many operations
  the interface exposes. A contract with a SINGLE operation can still warrant
  decomposition when its behavior describes several separable sub-steps or concerns.
- Return LEAF when the behavior is one cohesive computation a competent engineer
  would write as a single small module (a few branches are still a leaf).
- DECOMPOSE when the behavior names multiple separable stages or concerns that
  compose into the result (e.g. "do A, then B, then C", or "the result combines an
  X-check and a Y-check"). Author one child per concern and wire them with edges.
- Each child gets its OWN contract that YOU author — the child must be independently implementable.
- Children must form a DAG (no cycles). Edges declare which child depends on which.
- RECURSION: a child may ITSELF be a multi-step workflow. If so, author its
  contract at the right level of abstraction and the build system will RECURSE on
  it (calling you again for that child, which you may then decompose further). The
  engine enforces a hard depth cap, so you needn't track depth — but prefer the
  SHALLOWEST decomposition that cleanly separates concerns, and make a child a leaf
  as soon as it is directly implementable.
- Choose clean, descriptive child ids (lowercase, underscore-separated).
- Each child's contract must include: id, description, interface (operations with typed inputs/outputs), dependencies, behavior (prose).
- MODULE KINDS: a child is usually a STATELESS TRANSFORMATION (input→output, no memory).
  But a child may instead be a STATEFUL COMPONENT — one thing that HOLDS state across
  calls and offers several operations that share it (e.g. a store you can add to and
  later list). When a child's behavior is history-dependent (a later operation's result
  depends on earlier calls), it is a stateful component: keep its operations TOGETHER in
  ONE child (do NOT split add/list into separate modules — that would break the shared
  state) and mark that child "stateful": true. Use this ONLY when the behavior genuinely
  needs persisted state; do not prefer it. Most children remain stateless transformations.
- DEPENDENCY KINDS: there are two distinct ways one child can relate to another, and
  you express them DIFFERENTLY. Choose by what the behavior actually requires; neither
  is preferred.
  (a) DATAFLOW HANDOFF — one child's OUTPUT becomes the next child's INPUT, threaded in
      order. The downstream child stays a plain input→output module that knows nothing
      about the upstream one. Express this with an EDGE only ({from, to, name}); leave
      the downstream child's "dependencies" empty. The PARENT does the threading.
  (b) HELD CAPABILITY — a child is a UTILITY that one or more OTHER children must HOLD
      and CALL at points of their OWN choosing, on values only the caller has (e.g. a
      shared formatter/validator/logger a consumer calls on several values it computes
      itself). This is NOT a single value threaded once. Express it by listing the
      utility in EACH consumer child's "dependencies" as {"name": "<param>", "id":
      "<utility_id>"} AND adding an edge from the utility to each consumer. The consumer
      HOLDS the utility (constructor-injected) and calls it. A utility shared by several
      consumers is ONE child injected into each (do not duplicate it).

OUTPUT FORMAT (JSON):
For leaf: {"is_leaf": true}
For decompose: {
  "is_leaf": false,
  "children": [
    {
      "id": "<child_id>",
      "description": "<what it does>",
      "interface": {"operations": [{"name": "<op>", "inputs": {...}, "outputs": {...}, "errors": []}]},
      "dependencies": [],
      "behavior": [{"id": "<prop_id>", "prose": "<what must hold>"}],
      "stateful": false
    }
  ],
  "edges": [{"from": "<child_id>", "to": "<child_id>", "name": "<inject_param_name>"}]
}"""


def plan_canned(contract: dict) -> dict:
    """Always-returns-canned PLAN for the pipeline demo."""
    node_id = contract["id"]
    if node_id == "pipeline_demo":
        return CANNED_PIPELINE_DEMO_DECISION
    if node_id == "email_checker":
        return CANNED_FAN_IN_DECISION
    return {"is_leaf": True}


def plan(contract: dict, allow_decompose: bool = False) -> dict:
    """PLAN(contract) → decision.

    M-A/M-B/M-C: Canned decomposition for pipeline_demo; is_leaf:true for others.
    M-D: Real LLM call restricted to is_leaf:true.
    M-E: allow_decompose=True enables full decomposition (depth 1, pipeline-only).
    """
    node_id = contract["id"]

    # Canned: pipeline_demo always uses canned decomposition
    if node_id == "pipeline_demo":
        return CANNED_PIPELINE_DEMO_DECISION

    # Try real LLM PLAN if available
    if is_available():
        system = PLAN_SYSTEM_DECOMPOSE if allow_decompose else PLAN_SYSTEM_LEAF_ONLY
        contract_yaml = yaml.dump(contract, default_flow_style=False, sort_keys=False)
        user_prompt = f"""CONTRACT:
```yaml
{contract_yaml}
```

Decide: leaf or decompose?"""

        try:
            raw = call_with_retry(
                system_prompt=system,
                user_prompt=user_prompt,
                model=PLAN_MODEL,
                temperature=0.15 if allow_decompose else 0.05,
                max_tokens=2048 if allow_decompose else 256,
            )
            decision = parse_json_response(raw, context=f"PLAN({node_id})")

            if not allow_decompose:
                return {"is_leaf": True}

            # Validate decomposition
            if not decision.get("is_leaf", True):
                children = decision.get("children", [])
                edges = decision.get("edges", [])
                # Validate DAG (no cycles)
                _validate_dag(children, edges, node_id)
                # Validate child contracts have required fields
                _validate_child_contracts(children, node_id)
                return decision

            return {"is_leaf": True}

        except (LLMNotConfigured, LLMParseError) as e:
            print(f"  [plan] LLM failed for {node_id}, falling back to leaf: {e}")
        except ValueError as e:
            print(f"  [plan] Validation failed for {node_id}: {e}")
            # Fall back to leaf on validation failure
            return {"is_leaf": True}

    # Fallback: everything is a leaf
    return {"is_leaf": True}


def _validate_dag(children: list[dict], edges: list[dict], parent_id: str):
    """Validate that children+edges form a DAG (no cycles)."""
    child_ids = {c["id"] for c in children}
    # Build adjacency
    dep_of = {cid: set() for cid in child_ids}
    for edge in edges:
        frm = edge.get("from", "")
        to = edge.get("to", "")
        if frm not in child_ids:
            raise ValueError(f"Edge references unknown child '{frm}' in {parent_id}")
        if to not in child_ids:
            raise ValueError(f"Edge references unknown child '{to}' in {parent_id}")
        dep_of[to].add(frm)

    # Detect cycles via DFS
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {cid: WHITE for cid in child_ids}

    def dfs(cid):
        color[cid] = GRAY
        for dep in dep_of.get(cid, set()):
            if color[dep] == GRAY:
                raise ValueError(f"Cycle detected: {cid} → {dep} in decomposition of {parent_id}")
            if color[dep] == WHITE:
                dfs(dep)
        color[cid] = BLACK

    for cid in child_ids:
        if color[cid] == WHITE:
            dfs(cid)


def _validate_child_contracts(children: list[dict], parent_id: str):
    """Validate each child contract has required fields."""
    required = ["id", "description", "interface"]
    for child in children:
        for field in required:
            if field not in child:
                raise ValueError(f"Child contract missing '{field}' in decomposition of {parent_id}")
        iface = child.get("interface", {})
        ops = iface.get("operations", [])
        if not ops:
            raise ValueError(f"Child '{child['id']}' has no operations in decomposition of {parent_id}")


# ── Phase 11 Fix 2: dataflow shape-handoff block ───────────────────
# A downstream leaf consuming a sibling's dataflow OUTPUT is otherwise built knowing
# only its own contract (its input TYPE + prose) — never the concrete SHAPE of the
# value that will flow across the edge. So it guesses the upstream's encoding and can
# guess wrong (Phase 10 Probe B: the evaluator, blind to the parser's AST shape
# ["+", 3, ["*", 4, 2]], failed its own tests 3×). This threads a CONCRETE EXAMPLE of
# the upstream's real output into BOTH IMPLEMENT and DERIVE_TESTS, so the two halves —
# and the real upstream — agree on the shape BY CONSTRUCTION. It is the DATA analogue
# of Phase 7's capability-contract threading (same convention, not a parallel one).

def _inbound_examples_block(inbound_examples: dict | None) -> str:
    """Render the concrete-input-example block, or '' when there is none (so any
    non-dataflow / canned build is byte-for-byte unchanged)."""
    if not inbound_examples:
        return ""
    rendered = {}
    for src_id, example in inbound_examples.items():
        if example is None:
            continue
        try:
            rendered[src_id] = json.dumps(example, default=repr)
        except Exception:
            rendered[src_id] = repr(example)
    if not rendered:
        return ""
    body = "\n".join(f"  • from upstream stage '{sid}': {val[:1500]}"
                     + ("  …(truncated)" if len(val) > 1500 else "")
                     for sid, val in rendered.items())
    return f"""
CONCRETE INPUT EXAMPLE(S) — Phase 11 dataflow handoff. At assembly time the value(s)
flowing into THIS module from the previous, already-built and verified pipeline
stage(s) are shaped EXACTLY like the following. Build and test against THIS concrete
shape — do NOT guess the upstream's data shape from its declared type alone:
{body}
"""


# ── IMPLEMENT (real LLM from M-C) ──────────────────────────────────

IMPLEMENT_SYSTEM = """You are a code generator for a recursive agent build system called RICH.
You WRITE Python source and RETURN it as JSON text. You do NOT run code, you do NOT execute
anything, you do NOT search the web, you do NOT read or write files, you do NOT use ANY
tools whatsoever — emit ONLY the JSON object described at the end, as raw text. (Phase 11
Fix 1: reaching for a tool here strands the call; just write the code.)
Your job: given a module CONTRACT, produce the Python source code that satisfies it.

RULES:
1. Return ONLY valid Python source code (inside the JSON object below). No markdown
   fences, no prose, no tool calls.
2. SHAPE IS DECIDED BY DEPENDENCIES (see rule 4), not by preference. A leaf module
   with NO injected dependencies exports each operation as a TOP-LEVEL FUNCTION
   matching the operation name exactly (do NOT wrap it in a class). The test harness
   imports such functions directly.
   Example: def normalize(text: str) -> dict:
                return {"normalized": text.strip().lower()}
3. Each operation returns a dict matching its declared outputs.
4. INJECTED DEPENDENCIES decide the shape. Look at the DEPENDENCY CONTRACTS section:
   • If dependencies ARE listed there: write exactly ONE class whose __init__ receives
     each dependency BY NAME (the parameter name == the dependency name shown). Store
     them on self and call them via self.<name>.<op>(...). NEVER import a dependency
     and NEVER reimplement it. This is true whether the module is a sequential pipeline
     OR a module that merely HOLDS a utility and calls it — both receive deps the same way.
     Example: class MyConsumer:
                  def __init__(self, dep_a, dep_b):
                      self.dep_a = dep_a
                      self.dep_b = dep_b
                  def run(self, text):
                      r1 = self.dep_a.op(text)
                      return {"result": self.dep_b.op(r1["output_key"])["k"]}
   • If NO dependencies are listed: this is a leaf — follow rule 6.
5. HOW to use the injected dependencies depends on the PIPELINE MODE hint:
   • PIPELINE MODE True — the dependencies form a SEQUENTIAL PIPELINE: call the first
     dependency's operation, pass its output dict into the next, and so on.
   • PIPELINE MODE False but dependencies ARE present — they are HELD CAPABILITIES
     (utilities). Call them at the points YOUR OWN logic determines, on values YOU
     compute yourself (e.g. compute an intermediate result, then call the utility on
     it). They are NOT a single value threaded once — call the held handle wherever
     your computation needs it, as many times as needed.
   Either way: never reimplement a dependency; only call the injected handle.
6. LEAF MODE — ONLY when there are NO injected dependencies — two kinds, chosen by the
   contract's behavior, not by preference:
   (a) STATELESS TRANSFORMATION (default): the operations are pure input→output with no
       memory between calls. Export each operation as a top-level function (rule 2).
   (b) STATEFUL COMPONENT: the contract's behavior is HISTORY-DEPENDENT — an operation's
       result depends on what earlier operations did (e.g. something added is later
       listable; a counter advances). Implement this as a SINGLE class whose __init__
       sets up the internal state and whose methods (named exactly as the operations)
       read and mutate that state across calls. Each method still returns a dict matching
       its declared outputs. Define exactly ONE class in the module.
       Example: class Store:
                    def __init__(self): self._items = []
                    def add(self, text): self._items.append(text); return {"ok": True}
                    def list(self): return {"items": list(self._items)}
   Use (b) ONLY when the behavior genuinely requires state to persist across calls;
   otherwise use (a). The "STATEFUL: true/false" hint below tells you which the contract is.
7. If you receive failure output from a prior attempt, fix the bugs.

Output format: a JSON object with a single key "source" containing the Python code as a string."""


def implement_canned(contract: dict, dep_contracts=None, pipeline=False, prior_failures=None) -> str:
    """Always-returns-canned IMPLEMENT."""
    node_id = contract["id"]
    if node_id in CANNED_IMPLS:
        return CANNED_IMPLS[node_id]
    return ""


def implement(contract: dict, dep_contracts: dict | None = None,
              pipeline: bool = False, stateful: bool = False,
              prior_failures: list[str] | None = None,
              inbound_examples: dict | None = None) -> str:
    """IMPLEMENT(contract, dep_contracts, pipeline) → source code.

    M-A/M-B: Canned implementations for the pipeline demo.
    M-C onward: Real LLM call via OpenRouter. Falls back to canned if no API key.
    """
    dep_contracts = dep_contracts or {}
    node_id = contract["id"]

    # Fall back to canned for the pipeline demo
    if not is_available() and node_id in CANNED_IMPLS:
        return CANNED_IMPLS[node_id]

    if not is_available():
        raise LLMNotConfigured(
            "No API key available for IMPLEMENT. "
            "Set OPENROUTER_API_KEY or provide canned implementations."
        )

    # Build the prompt
    contract_yaml = yaml.dump(contract, default_flow_style=False, sort_keys=False)
    dep_yaml = ""
    if dep_contracts:
        dep_yaml = yaml.dump(dep_contracts, default_flow_style=False, sort_keys=False)

    user_prompt = f"""CONTRACT:
```yaml
{contract_yaml}
```

DEPENDENCY CONTRACTS (interfaces only, never source):
```yaml
{dep_yaml if dep_yaml else "(none — this is a leaf module with no dependencies)"}
```

PIPELINE MODE: {pipeline}
STATEFUL: {stateful}
"""

    # Phase 11 Fix 2: thread the concrete upstream-output shape (if any) into IMPLEMENT.
    user_prompt += _inbound_examples_block(inbound_examples)

    if prior_failures:
        user_prompt += f"""
PRIOR ATTEMPT FAILURES (fix these):
{chr(10).join(prior_failures)}
"""

    # Harness hardening (Phase 7): the subagent backend occasionally returns an
    # empty / non-JSON envelope (observed: a blank DERIVE_TESTS/IMPLEMENT response that
    # raises LLMParseError "Expecting value: line 1 column 1"). That is transient, not a
    # contract problem — retry a couple times before falling back. Orthogonal to the
    # leaf-injection fix; keeps a long live build from aborting on one flaky call.
    last_err = None
    for _try in range(3):
        try:
            raw = call_with_retry(
                system_prompt=IMPLEMENT_SYSTEM,
                user_prompt=user_prompt,
                model=IMPL_MODEL,
                temperature=0.1,
                max_tokens=4096,
            )
            result = parse_json_response(raw, context=f"IMPLEMENT({node_id})")
            # Harden (Phase 11): a tool_use/max_turns-garbled retry can parse to valid JSON
            # that LACKS a 'source' key. Treat that as a parse failure so the retry loop /
            # canned fallback handle it — never let a bare KeyError crash the whole build.
            if not isinstance(result, dict) or not isinstance(result.get("source"), str) \
                    or not result["source"].strip():
                keys = list(result.keys()) if isinstance(result, dict) else type(result).__name__
                raise LLMParseError(f"IMPLEMENT({node_id}) response has no usable 'source' "
                                    f"(got: {keys})")
            return result["source"]
        except LLMParseError as e:
            last_err = e
            print(f"  [implement] parse failure for {node_id} (try {_try + 1}/3), retrying: {e}")
            continue
        except LLMNotConfigured as e:
            last_err = e
            break
    # If LLM fails and we have a canned fallback, use it
    if node_id in CANNED_IMPLS:
        print(f"  [implement] LLM failed for {node_id}, using canned fallback: {last_err}")
        return CANNED_IMPLS[node_id]
    raise last_err


# ── DERIVE_TESTS (real LLM from M-C) ────────────────────────────────

DERIVE_TESTS_SYSTEM = """You are a test generator for a recursive agent build system called RICH.
You WRITE a pytest file and RETURN it as JSON text. You do NOT run code, you do NOT execute
anything, you do NOT search the web, you do NOT read or write files, you do NOT use ANY
tools whatsoever — emit ONLY the JSON object described at the end, as raw text. (Phase 11
Fix 1: reaching for a tool here strands the call; just write the tests.)
Your job: given a module CONTRACT, produce a pytest test file that verifies the implementation.

RULES:
1. Return ONLY valid Python pytest source (inside the JSON object below). No markdown
   fences, no prose, no tool calls.
2. Import the module by name (from <module_id> import <op_name>).
3. Test every operation in contract.interface.operations.
4. CRITICAL: call each operation with its INPUTS as KEYWORD ARGUMENTS named exactly as
   declared — e.g. for inputs {amount: number} call op(amount=1234.5). Do NOT wrap the
   inputs in a single dict: op({"amount": 1234.5}) is WRONG. Operations RETURN a dict
   with keys matching the declared outputs — extract the right key before asserting.
   Example: result = op(amount=1234.5); assert result["output_key"] == expected
5. For each operation: test normal inputs, edge cases, and declared error conditions.
6. Tests are consumer-driven — they encode what the consumer needs from this module.
7. Use descriptive test names: test_<op>_<scenario>.

Output format: a JSON object with a single key "tests" containing the pytest code as a string."""


# Fix 1 (M-H): internal/pipeline nodes are NOT top-level functions — they expose a
# single injected wiring class. This addendum OVERRIDES rule 2 of DERIVE_TESTS_SYSTEM
# and tells the generator to discover that class by introspection (so the test does
# not depend on the exact class name IMPLEMENT happens to pick) and to inject FAKE
# dependencies that honor the dependency contracts (assume-guarantee verification).
DERIVE_TESTS_INTERNAL_ADDENDUM = """

MODULE WITH INJECTED DEPENDENCIES (an internal/pipeline node, OR a leaf that HOLDS a
utility and calls it) — THIS OVERRIDES RULE 2 ABOVE.
The module under test does NOT export top-level functions. It defines exactly ONE
class (the "wiring class") whose __init__ receives the dependencies listed below
BY NAME. Your test MUST:

  1. Discover the wiring class by introspection — do NOT hard-code its name and do
     NOT import any operation as a top-level function:

         import importlib, inspect
         _mod = importlib.import_module("{module_id}")
         WiringClass = next(c for _n, c in inspect.getmembers(_mod, inspect.isclass)
                            if c.__module__ == "{module_id}")

  2. Build a FAKE object for each dependency below whose method(s) return dicts
     matching that dependency's declared OUTPUT keys. Keep them trivial:

         class _Fake: pass
         dep = _Fake(); dep.<dep_op> = lambda *a, **k: {<that op's declared outputs>}

  3. Construct the wiring with the fakes injected BY NAME (the __init__ parameter
     names are exactly the dependency names below):

         w = WiringClass({dep_kwargs})

  4. Call this node's own operation(s) on `w` and assert on the returned dict.
     This is assume-guarantee verification: assume each dependency honors its
     contract (that is what the fakes encode) and verify only that THIS node wires
     them correctly. Drive the fakes with known outputs and assert the composed
     output reflects the data-flow the contract implies (e.g. the final stage's
     value is what surfaces). If a dependency is a HELD UTILITY (not a sequential
     pipeline stage), assert that THIS node CALLS it on the values it computes
     itself and that the utility's result surfaces in this node's output.

  5. STATEFUL DEPENDENCY: if a dependency's contract is marked stateful (its behavior
     is history-dependent), a canned-return fake is WRONG — it cannot honor
     "added-then-listable". Make that dependency's fake a small class that HOLDS state,
     so a sequence through THIS node (e.g. add via the dep, then read via this node)
     reflects history. Then test THIS node across an operation SEQUENCE, not one call.
     Example stateful fake:
         class _FakeStore:
             def __init__(self): self._items = []
             def add(self, text): self._items.append({"text": text, "done": False}); return {"id": len(self._items)}
             def list(self): return {"items": list(self._items)}"""


# Phase 6: a STATEFUL leaf is a single class whose behavior depends on history. Its
# tests cannot be single input→output asserts — they must drive a SEQUENCE of
# operations and assert on state-dependent results. This addendum OVERRIDES rule 2
# (no top-level function import) and rule 4's single-call shape.
DERIVE_TESTS_STATEFUL_ADDENDUM = """

STATEFUL COMPONENT — THIS OVERRIDES RULES 2 AND 4 ABOVE.
The module under test does NOT export top-level functions. It defines exactly ONE
class that holds state across calls. Its behavior is HISTORY-DEPENDENT: an operation's
result depends on operations called before it. Your test MUST:

  1. Discover the class by introspection (do NOT hard-code its name):

         import importlib, inspect
         _mod = importlib.import_module("{module_id}")
         Comp = next(c for _n, c in inspect.getmembers(_mod, inspect.isclass)
                     if c.__module__ == "{module_id}")

  2. Write TRACE tests: instantiate ONE component, call a SEQUENCE of operations, and
     assert on results that depend on the prior calls — not single isolated calls.
     Each behavior property in the contract is a trace invariant; realize it as a
     sequence. Examples (shape, not literal):

         c = Comp()
         rid = c.add(text="milk")["id"]
         assert any(i["text"] == "milk" for i in c.list()["items"])   # add-then-list
         c.complete(id=rid)
         assert all(i["done"] for i in c.list()["items"] if i["id"] == rid)  # complete-then-list

  3. Use a FRESH instance per test so tests do not leak state into each other.
  4. Cover the history-dependent properties (added-things-are-listable, mutation-is-
     visible-on-later-reads, ids/keys are stable/unique across calls) — these are the
     properties single-call tests cannot express."""


def derive_tests_canned(contract: dict) -> str:
    """Always-returns-canned DERIVE_TESTS."""
    node_id = contract["id"]
    if node_id in CANNED_TESTS:
        return CANNED_TESTS[node_id]
    return ""


def derive_tests(contract: dict, dep_contracts: dict | None = None,
                 pipeline: bool = False, stateful: bool = False,
                 inbound_examples: dict | None = None) -> str:
    """DERIVE_TESTS(contract[, dep_contracts, pipeline]) → pytest source.

    M-A/M-B: Canned test files for the pipeline demo.
    M-C onward: Real LLM call via OpenRouter. Falls back to canned if no API key.

    Fix 1 (M-H): for an internal/pipeline node (``pipeline=True``) the module is a
    single injected wiring class, not top-level functions. We thread the dependency
    contracts in and switch the prompt so the generated test discovers that class by
    introspection and injects FAKE dependencies (assume-guarantee). ``dep_contracts``
    is the same ``{name: contract}`` dict IMPLEMENT receives, so test and impl agree
    on dependency names and output keys.
    """
    node_id = contract["id"]
    dep_contracts = dep_contracts or {}

    if not is_available() and node_id in CANNED_TESTS:
        return CANNED_TESTS[node_id]

    if not is_available():
        raise LLMNotConfigured(
            "No API key available for DERIVE_TESTS. "
            "Set OPENROUTER_API_KEY or provide canned tests."
        )

    contract_yaml = yaml.dump(contract, default_flow_style=False, sort_keys=False)

    # Phase 7: route on dep PRESENCE, not the pipeline flag. ANY module that receives
    # injected dependencies — an internal/pipeline node OR a leaf that holds a utility —
    # is a single class verified against contract-derived fakes (assume-guarantee). This
    # is the same machinery; a held-capability leaf now reaches it (closing the gap).
    if dep_contracts:
        dep_names = list(dep_contracts.keys())
        dep_kwargs = ", ".join(f"{n}=<fake_{n}>" for n in dep_names)
        # .replace (not .format) — the addendum contains literal braces in its
        # code examples that are not placeholders.
        addendum = (DERIVE_TESTS_INTERNAL_ADDENDUM
                    .replace("{module_id}", node_id)
                    .replace("{dep_kwargs}", dep_kwargs))
        system_prompt = DERIVE_TESTS_SYSTEM + addendum
        dep_yaml = yaml.dump(dep_contracts, default_flow_style=False, sort_keys=False)
        user_prompt = f"""CONTRACT (a module that receives injected dependencies):
```yaml
{contract_yaml}
```

DEPENDENCY CONTRACTS (interfaces only — these are the injected deps; fake them):
```yaml
{dep_yaml}
```

The module id is '{node_id}'. It contains ONE wiring class that receives these
dependencies by name. Generate a pytest file per the INTERNAL / PIPELINE NODE rules."""
    elif stateful:
        system_prompt = DERIVE_TESTS_SYSTEM + DERIVE_TESTS_STATEFUL_ADDENDUM.replace(
            "{module_id}", node_id)
        user_prompt = f"""CONTRACT (a STATEFUL component — its behavior is history-dependent):
```yaml
{contract_yaml}
```

The module id is '{node_id}'. It defines ONE class holding state across calls. Generate a
pytest file of TRACE tests (operation sequences) per the STATEFUL COMPONENT rules."""
    else:
        system_prompt = DERIVE_TESTS_SYSTEM
        user_prompt = f"""CONTRACT:
```yaml
{contract_yaml}
```

Generate a pytest file that imports from '{node_id}' and tests all operations."""

    # Phase 11 Fix 2: thread the SAME concrete upstream-output shape that IMPLEMENT
    # receives, so test and impl exercise the identical input shape (agree by construction).
    user_prompt += _inbound_examples_block(inbound_examples)

    # Harness hardening (Phase 7): retry transient empty/non-JSON subagent envelopes
    # (LLMParseError) a couple times before falling back — see implement() for the why.
    last_err = None
    for _try in range(3):
        try:
            raw = call_with_retry(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                model=TESTS_MODEL,
                temperature=0.1,
                max_tokens=4096,
            )
            result = parse_json_response(raw, context=f"DERIVE_TESTS({node_id})")
            # Harden (Phase 11): a garbled retry can parse to valid JSON lacking 'tests';
            # treat as a parse failure so the retry loop / canned fallback handle it.
            if not isinstance(result, dict) or not isinstance(result.get("tests"), str) \
                    or not result["tests"].strip():
                keys = list(result.keys()) if isinstance(result, dict) else type(result).__name__
                raise LLMParseError(f"DERIVE_TESTS({node_id}) response has no usable 'tests' "
                                    f"(got: {keys})")
            return result["tests"]
        except LLMParseError as e:
            last_err = e
            print(f"  [derive_tests] parse failure for {node_id} (try {_try + 1}/3), retrying: {e}")
            continue
        except LLMNotConfigured as e:
            last_err = e
            break
    if node_id in CANNED_TESTS:
        print(f"  [derive_tests] LLM failed for {node_id}, using canned fallback: {last_err}")
        return CANNED_TESTS[node_id]
    raise last_err


# ── DERIVE_TESTS — INTEGRATION mode (Phase 8) ──────────────────────────
# When an internal node's children SHARE A STATEFUL DEPENDENCY (one mutable component used
# by >1 sibling writer), per-module unit tests fake that dependency and CANNOT see the
# interaction through shared state (the frame rule's disjoint-footprint side-condition is
# violated). This mode generates a test over the REAL assembled subtree (no fakes) running
# an INTERLEAVED multi-writer sequence, asserting the node's interaction invariants. It is
# ADDED to — never replaces — the per-module unit tests.

DERIVE_TESTS_INTEGRATION_SYSTEM = """You are a test generator for the RICH build system. You WRITE a pytest file as text and
return it as JSON. You do NOT run code, you do NOT inspect files, you do NOT use any tools —
emit only the JSON object described at the end.

The internal node under test composes children that SHARE A STATEFUL DEPENDENCY — one
mutable component used by MULTIPLE sibling writers. Per-module unit tests fake that
dependency and therefore cannot observe interactions THROUGH the shared state. Write an
INTEGRATION test over the REAL assembled subtree (NO fakes) that runs an INTERLEAVED
sequence spanning MULTIPLE writers through the shared state and asserts the node's
interaction invariants (its behavior properties).

RULES:
1. Return ONLY valid pytest source (no markdown fences, no prose, no tool use).
2. Each test you WRITE begins with these two lines (this is CODE your test contains — you
   are writing it, not executing it):
       from main import assemble
       app = assemble()
   `app` is the composed node; its shared stateful dependency is ONE real instance shared by
   all writers. Do not fake or construct anything else.
3. Interleave operations ACROSS writers through the shared state: change state via one
   operation, then act via a DIFFERENT operation that must observe that change. A
   single-writer sequence does NOT exercise the interaction — that is the whole point.
4. Assert the node's BEHAVIOR invariants over the sequence (e.g. "a withdraw sees prior
   deposits", "the balance never goes negative across writers"). Turn each behavior property
   into an interleaved trace, the way a stateful module's behavior becomes a sequence test.
5. Use a FRESH `app = assemble()` per test so tests do not leak state.
6. Do NOT coach a particular fix — let the invariant determine what to assert.

Output format: a JSON object with a single key "tests" whose value is the pytest source as a string."""


def derive_integration_test(contract: dict, prior_failures: list[str] | None = None) -> str:
    """Generate an integration trace test (real assembled subtree) for an internal node
    whose children share a stateful dependency. Phase 8 verification mechanism."""
    node_id = contract["id"]
    if not is_available():
        raise LLMNotConfigured("No API key for integration DERIVE_TESTS.")
    contract_yaml = yaml.dump(contract, default_flow_style=False, sort_keys=False)
    user_prompt = f"""INTERNAL NODE CONTRACT (its children share a stateful dependency):
```yaml
{contract_yaml}
```

The real composed system is obtained with `from main import assemble; app = assemble()`.
Generate an INTEGRATION test: an interleaved multi-writer sequence over the REAL subtree
asserting the behavior invariants above."""
    if prior_failures:
        user_prompt += f"\n\nPRIOR ATTEMPT FAILURES (fix these):\n{chr(10).join(prior_failures)}"

    last_err = None
    for _try in range(3):
        try:
            raw = call_with_retry(system_prompt=DERIVE_TESTS_INTEGRATION_SYSTEM,
                                  user_prompt=user_prompt, model=TESTS_MODEL,
                                  temperature=0.1, max_tokens=4096)
            return parse_json_response(raw, context=f"DERIVE_INTEGRATION({node_id})")["tests"]
        except LLMParseError as e:
            last_err = e
            print(f"  [derive_integration] parse failure for {node_id} (try {_try + 1}/3): {e}")
            continue
        except LLMNotConfigured as e:
            last_err = e
            break
    raise last_err


# ═════════════════════════════════════════════════════════════════════
# Canned data (M-A/M-B fallback for pipeline demo)
# ═════════════════════════════════════════════════════════════════════

CANNED_PIPELINE_DEMO_DECISION = {
    "is_leaf": False,
    "children": [
        {
            "id": "normalizer",
            "description": "Normalize a string: strip whitespace, lowercase",
            "interface": {
                "operations": [
                    {
                        "name": "normalize",
                        "inputs": {"text": "string"},
                        "outputs": {"normalized": "string"},
                        "errors": [],
                    }
                ]
            },
            "dependencies": [],
            "behavior": [
                {
                    "id": "strip_and_lower",
                    "prose": "Normalized text must have no leading/trailing whitespace and be lowercase",
                }
            ],
        },
        {
            "id": "validator",
            "description": "Validate a normalized string: non-empty, no special characters",
            "interface": {
                "operations": [
                    {
                        "name": "validate",
                        "inputs": {"text": "string"},
                        "outputs": {"valid": "bool", "reason": "string"},
                        "errors": [],
                    }
                ]
            },
            "dependencies": [],
            "behavior": [
                {"id": "non_empty", "prose": "Empty strings are invalid"},
                {"id": "no_special_chars", "prose": "Strings with special characters other than letters/digits/spaces are invalid"},
            ],
        },
    ],
    "edges": [{"from": "normalizer", "to": "validator", "name": "normalized"}],
}


# M-F: Fan-in demo — two children share one dependency
CANNED_FAN_IN_DECISION = {
    "is_leaf": False,
    "children": [
        {
            "id": "regex_engine",
            "description": "Provide regex pattern matching: check if a string matches a given pattern",
            "interface": {
                "operations": [
                    {
                        "name": "matches",
                        "inputs": {"text": "string", "pattern": "string"},
                        "outputs": {"ok": "bool", "match": "string"},
                        "errors": [],
                    }
                ]
            },
            "dependencies": [],
            "behavior": [
                {"id": "match_or_not", "prose": "Returns ok=true and the matched text if pattern matches, ok=false and empty match otherwise"},
            ],
        },
        {
            "id": "format_checker",
            "description": "Check if an email has valid format (contains @, has domain part)",
            "interface": {
                "operations": [
                    {
                        "name": "check_format",
                        "inputs": {"email": "string"},
                        "outputs": {"valid": "bool"},
                        "errors": [],
                    }
                ]
            },
            "dependencies": [{"name": "regex", "id": "regex_engine"}],
            "behavior": [
                {"id": "has_at", "prose": "Email must contain exactly one @ sign"},
                {"id": "has_domain", "prose": "Domain part after @ must be non-empty"},
            ],
        },
        {
            "id": "domain_checker",
            "description": "Check if email domain is a common provider (gmail, yahoo, outlook)",
            "interface": {
                "operations": [
                    {
                        "name": "is_common",
                        "inputs": {"email": "string"},
                        "outputs": {"common": "bool", "domain": "string"},
                        "errors": [],
                    }
                ]
            },
            "dependencies": [{"name": "regex", "id": "regex_engine"}],
            "behavior": [
                {"id": "common_providers", "prose": "Returns common=true for gmail.com, yahoo.com, outlook.com domains"},
            ],
        },
    ],
    "edges": [
        {"from": "regex_engine", "to": "format_checker", "name": "regex"},
        {"from": "regex_engine", "to": "domain_checker", "name": "regex"},
    ],
}

CANNED_IMPLS = {
    "normalizer": '''"""Normalize a string: strip whitespace, lowercase."""


def normalize(text: str) -> dict:
    """Return {"normalized": <string>} after stripping whitespace and lowercasing."""
    return {"normalized": text.strip().lower()}
''',
    "validator": '''"""Validate a normalized string: non-empty, no special characters."""


def validate(text: str) -> dict:
    """Return {"valid": bool, "reason": str} after validation checks."""
    if not text:
        return {"valid": False, "reason": "String is empty"}
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789 ")
    for ch in text:
        if ch not in allowed:
            return {"valid": False, "reason": "String contains special characters"}
    return {"valid": True, "reason": "OK"}
''',
    "pipeline_demo": '''"""Pipeline demo: normalize → validate.

Receives normalizer + validator as injected dependencies.
"""


class PipelineDemo:
    def __init__(self, normalizer, validator):
        self.normalizer = normalizer
        self.validator = validator

    def run(self, text: str) -> dict:
        """Run the pipeline: normalize then validate."""
        norm_result = self.normalizer.normalize(text)
        val_result = self.validator.validate(norm_result["normalized"])
        return {
            "original": text,
            "normalized": norm_result["normalized"],
            "valid": val_result["valid"],
            "reason": val_result["reason"],
        }
''',
    # M-F: Fan-in canned implementations
    "regex_engine": '''"""Regex engine: pattern matching via Python's re module."""

import re


def matches(text: str, pattern: str) -> dict:
    """Check if text matches pattern. Returns ok=True and the match if found."""
    m = re.search(pattern, text)
    if m:
        return {"ok": True, "match": m.group()}
    return {"ok": False, "match": ""}
''',
    "format_checker": '''"""Format checker: validate email format using injected regex engine."""


class FormatChecker:
    def __init__(self, regex):
        self.regex = regex

    def check_format(self, email: str) -> dict:
        """Check email has valid format: contains @, has domain part."""
        result = self.regex.matches(email, r"^[^@]+@[^@]+\\.[^@]+$")
        return {"valid": result["ok"]}
''',
    "domain_checker": '''"""Domain checker: check if email domain is common using injected regex engine."""


class DomainChecker:
    def __init__(self, regex):
        self.regex = regex

    def is_common(self, email: str) -> dict:
        """Check if email domain is gmail/yahoo/outlook."""
        result = self.regex.matches(email, r"@(gmail|yahoo|outlook)\\.")
        if result["ok"]:
            return {"common": True, "domain": result["match"].lstrip("@").rstrip(".")}
        return {"common": False, "domain": ""}
''',
    "email_checker": '''"""Email checker: compose format_checker and domain_checker over shared regex_engine."""


class EmailChecker:
    def __init__(self, regex_engine, format_checker, domain_checker):
        self.regex_engine = regex_engine
        self.format_checker = format_checker
        self.domain_checker = domain_checker

    def check(self, email: str) -> dict:
        """Check email format and domain. Both checkers share the same regex engine."""
        fmt = self.format_checker.check_format(email)
        dom = self.domain_checker.is_common(email)
        return {
            "email": email,
            "valid_format": fmt["valid"],
            "common_domain": dom["common"],
            "domain": dom["domain"],
        }
''',
}

CANNED_TESTS = {
    "normalizer": '''"""Tests for normalizer — derived from contract."""

from normalizer import normalize


def test_normalize_strips_whitespace():
    result = normalize("  hello  ")
    assert result["normalized"] == "hello"


def test_normalize_lowercases():
    result = normalize("HELLO")
    assert result["normalized"] == "hello"


def test_normalize_empty_string():
    result = normalize("")
    assert result["normalized"] == ""


def test_normalize_already_clean():
    result = normalize("hello world")
    assert result["normalized"] == "hello world"
''',
    "validator": '''"""Tests for validator — derived from contract."""

from validator import validate


def test_validate_normal_string():
    result = validate("hello world")
    assert result["valid"] is True
    assert result["reason"] == "OK"


def test_validate_empty_string():
    result = validate("")
    assert result["valid"] is False
    assert result["reason"] == "String is empty"


def test_validate_special_chars():
    result = validate("hello@world")
    assert result["valid"] is False
    assert result["reason"] == "String contains special characters"


def test_validate_numeric():
    result = validate("hello 123")
    assert result["valid"] is True
''',
    "pipeline_demo": '''"""Tests for pipeline_demo — integration-level."""

from pipeline_demo import PipelineDemo


class FakeNormalizer:
    def normalize(self, text):
        return {"normalized": text.strip().lower()}


class FakeValidator:
    def validate(self, text):
        if not text:
            return {"valid": False, "reason": "empty"}
        return {"valid": True, "reason": "OK"}


def test_pipeline_happy_path():
    demo = PipelineDemo(FakeNormalizer(), FakeValidator())
    result = demo.run("  Hello World  ")
    assert result["original"] == "  Hello World  "
    assert result["normalized"] == "hello world"
    assert result["valid"] is True
''',
    "regex_engine": '''"""Tests for regex_engine."""

from regex_engine import matches


def test_matches_found():
    result = matches(text="hello world", pattern=r"hello")
    assert result["ok"] is True
    assert result["match"] == "hello"


def test_matches_not_found():
    result = matches(text="hello world", pattern=r"xyz")
    assert result["ok"] is False
    assert result["match"] == ""


def test_matches_email():
    result = matches(text="user@gmail.com", pattern=r"@gmail\\.")
    assert result["ok"] is True
''',
    "format_checker": '''"""Tests for format_checker — uses fake regex engine."""

from format_checker import FormatChecker


class FakeRegex:
    def matches(self, text, pattern):
        if "@" in text and "." in text.split("@")[-1]:
            return {"ok": True, "match": text}
        return {"ok": False, "match": ""}


def test_valid_email():
    checker = FormatChecker(FakeRegex())
    result = checker.check_format("user@gmail.com")
    assert result["valid"] is True


def test_invalid_email_no_at():
    checker = FormatChecker(FakeRegex())
    result = checker.check_format("usergmail.com")
    assert result["valid"] is False
''',
    "domain_checker": '''"""Tests for domain_checker — uses fake regex engine."""

from domain_checker import DomainChecker


class FakeRegex:
    def matches(self, text, pattern):
        if "gmail" in text:
            return {"ok": True, "match": "@gmail."}
        return {"ok": False, "match": ""}


def test_common_domain():
    checker = DomainChecker(FakeRegex())
    result = checker.is_common("user@gmail.com")
    assert result["common"] is True
    assert result["domain"] == "gmail"


def test_uncommon_domain():
    checker = DomainChecker(FakeRegex())
    result = checker.is_common("user@company.com")
    assert result["common"] is False
''',
    "email_checker": '''"""Tests for email_checker — integration with shared regex_engine."""

from email_checker import EmailChecker
from regex_engine import matches as real_matches


class RealRegex:
    def matches(self, text, pattern):
        return real_matches(text, pattern)


class FakeFormatChecker:
    def __init__(self, regex):
        self.regex = regex
    def check_format(self, email):
        r = self.regex.matches(email, r"@")
        return {"valid": r["ok"]}


class FakeDomainChecker:
    def __init__(self, regex):
        self.regex = regex
    def is_common(self, email):
        r = self.regex.matches(email, r"@gmail")
        return {"common": r["ok"], "domain": "gmail" if r["ok"] else ""}


def test_valid_gmail():
    regex = RealRegex()
    checker = EmailChecker(regex, FakeFormatChecker(regex), FakeDomainChecker(regex))
    result = checker.check("user@gmail.com")
    assert result["valid_format"] is True
    assert result["common_domain"] is True
''',
}
