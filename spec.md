# RICH — Recursive Agent Build System — v1 Spec

> Hand this entire file to the implementing agent as the project brief. It is written to be executed top to bottom. You do not have the design conversation that produced this; everything you need is here. **Do not infer, extend, or "improve" beyond what is written. When this spec states a decision, it is a decision — implement it, do not relitigate it.** Where you think something is wrong, leave a `# SPEC-NOTE:` comment and continue; do not silently change course.

---

## 0. Rules of engagement (read first, obey throughout)

1. **Build in milestone order M-A → M-H.** Each milestone has an acceptance check. Do not start a milestone until the previous one's check passes. Commit after each.
2. **The locked decisions in §3 are not open for relitigation.** They are load-bearing for parts of the system you cannot see yet. Honor them in every milestone, including ones that only matter later.
3. **Respect the non-goals in §9.** When in doubt, build less. The most common way to wreck this project is to build a later milestone's complexity into an earlier one.
4. **The riskiest, most important component is the PLAN skill (§5.1).** Everything else is mechanical scaffolding. Build the scaffolding to be *trustworthy and deterministic* so that when PLAN's variance enters, you can isolate it.
5. **Determinism before intelligence.** The verification and assembly machinery (§6) must be real and solid *before* any real LLM call is wired in. This ordering is mandatory and is the single most important sequencing decision in the spec.

---

## 1. What you are building, in one paragraph

A system that takes a single high-level goal and recursively decomposes it into a tree of **modules**, where each module is built by an LLM agent. A module is either a **leaf** (implemented directly as code) or **internal** (decomposed into child modules and implemented as *wiring* that composes its children). Each module is specified by a **contract** that is **authored by its parent** (never by itself). Each module's implementation is written against its dependencies' **contracts only**, never their source. Modules are verified against their contracts by **running tests derived from the contract**. The finished tree is **assembled** by a deterministic topological fold that injects each module's dependencies into it, producing a runnable deliverable. The entire system is one recursive procedure (§4) plus three LLM skills (§5) plus two deterministic engines (§6).

---

## 2. The central idea you must internalize (and three traps it sets)

**Contracts flow DOWN from demand, not UP from supply.** A module's contract describes what it must *provide to its consumer*. The consumer (the parent) authors it. A module never writes its own contract — it receives it as its task and is responsible only for *satisfying* it. The only contract authored "from outside" is the root module's, authored from the user goal (you provide this at the top; see §5.0).

This single rule is why the system has no global "reconciliation" component, and you must not build one. Three consequences, each of which is a trap if you forget it:

- **Trap 1 — do not let a module author its own contract.** When an internal module decomposes into children, *that act of decomposition IS the authoring of the children's contracts*. The child receives its contract; it does not write it. (See §3-D1, §4.)
- **Trap 2 — do not build a "compiler agent" or any intelligent assembler.** Assembly is a *deterministic* topological fold (§6.2). If assembly ever appears to need a judgment call, that is a **diagnostic of an underspecified contract**, not a reason to add intelligence. Fix the contract upstream. (See §3-D6.)
- **Trap 3 — do not build a filesystem firewall/sandbox for v1.** Because each implementation call is a single stateless LLM call, the boundary is enforced *by what you put in the prompt*. You simply never include a dependency's source — only its contract. The context IS the boundary. (See §3-D5, §6.3.)

---

## 3. Locked decisions (do not relitigate)

**D1 — A node's contract is authored by its parent.** The root's contract is authored from the user goal. No node ever authors its own contract. When PLAN decomposes a node, its output includes the full contracts of all children.

**D2 — A node is leaf XOR internal.** Leaf = implemented directly as source. Internal = decomposed into children + implemented as wiring that composes those children. The choice is PLAN's, governed by the budget (D7).

**D3 — Implementation is written against dependency CONTRACTS, never dependency source.** This holds identically for leaves (zero deps) and internal nodes (deps = children). There is exactly one implementation skill, parameterized by how many dependency contracts it is handed.

**D4 — Dependencies are injected by NAME, not imported.** A module receives its dependencies as named handles (e.g. constructor/factory parameters). It must not `import` another module. The name-keying is what makes assembly deterministic (if two deps share a shape, the name disambiguates which goes where).

**D5 — The firewall for v1 is the prompt.** Implementation is a single stateless LLM call. The boundary is enforced by assembling a prompt that contains the node's own contract + its dependencies' contracts, and *no dependency source*. Do not build materialized trees, file-tool sandboxes, or OS jails in v1.

**D6 — Assembly is deterministic.** A topological fold that instantiates leaves and injects dependencies upward by name. The "compiler" output is a generated entrypoint that performs this fold. No LLM is involved in assembly. A shared dependency (one node, multiple in-edges) is instantiated exactly once and injected into all its consumers.

**D7 — The budget is the recursion's base case.** PLAN decides leaf-vs-decompose by whether the contract is small enough to implement directly within budget. Budget is configurable (default: see §5.2). This is enforced twice: PLAN's judgment (soft), and a post-implementation size check (hard).

**D8 — Verification in v1 is running consumer-derived tests.** Tests are derived from the contract (which the consumer authored), so they encode what the consumer needs. Passing tests means "not observed to violate the contract on tested inputs" — this is existential, not a proof. Do not claim or imply it is a proof. Do not build SMT/formal verification in v1 (see §9).

**D9 — v1 wiring is PIPELINE-ONLY.** Internal-node wiring composes children as a sequential/dataflow pipeline (output of one feeds the next, or simple aggregation). Conditional routing, error-branching, partial rollback, and loops are explicitly out of scope for v1 (see §9). Choose example goals accordingly.

---

## 4. The core recursive procedure (the whole system)

Implement this as the spine. Pseudocode — translate faithfully; the control flow, including failure propagation, is the specification.

```
build(contract) -> Node | FAILURE:
    decision = PLAN(contract)                         # LLM skill (§5.1); stubbed until M-D
    persist decision.json, status=planned

    if decision.is_leaf:
        tests = DERIVE_TESTS(contract)                # LLM skill (§5.3); stubbed until M-C
        persist tests/
        for attempt in 1..K_IMPL:                     # K_IMPL default 3
            src = IMPLEMENT(contract, dep_contracts={})# LLM skill (§5.2); stubbed until M-C
            result = run_tests(src, tests)            # DETERMINISTIC (§6.1)
            if result.passed:
                persist src/, status=verified
                return Leaf(contract, src, tests)
            # else: include result.failures in the next IMPLEMENT attempt's prompt
        return FAILURE(contract, reason="leaf unsatisfiable after K_IMPL attempts")

    else:  # internal node
        # decision.children = list of child contracts (AUTHORED BY THIS NODE'S PLAN)
        # decision.edges    = dependency edges among children (by name)
        children = {}
        for child_contract in topological_order(decision.children, decision.edges):
            node = build(child_contract)              # RECURSE
            if node is FAILURE:
                if replans_remaining > 0:             # REPLAN only exists from M-G
                    decision = REPLAN(contract, failed=child_contract, reason=node.reason)
                    persist decision.json
                    restart this branch with new decision
                else:
                    return FAILURE(contract, reason="decomposition unsatisfiable: " + node.reason)
            children[child_contract.id] = node

        tests = DERIVE_TESTS(contract)
        persist tests/
        for attempt in 1..K_WIRE:                     # K_WIRE default 3
            # dep_contracts here are the CHILDREN'S contracts — never their source
            src = IMPLEMENT(contract, dep_contracts={child contracts}, pipeline=True)
            result = run_tests(src, tests)
            if result.passed:
                persist src/, status=verified
                return Internal(contract, src, children, decision.edges)
        return FAILURE(contract, reason="wiring failed after K_WIRE attempts")
```

Key invariants the code must preserve:
- **Ratification is emergent, not a separate step.** "Consumer finds contract sufficient" is automatic (the consumer authored it). "Provider finds contract satisfiable" is *proven by `build` succeeding* / *refuted by it returning FAILURE*. Do not build a separate negotiation/handshake protocol. Backtracking (REPLAN on child FAILURE) IS the negotiation.
- **Leaf implementation and internal wiring call the SAME skill** (IMPLEMENT), differing only in dep count and the `pipeline` flag.
- **Memoize** (from M-G): a verified node whose contract is unchanged must never be rebuilt.

---

## 5. The LLM skills (the only non-deterministic parts)

There are exactly three skills for v1 (PLAN, IMPLEMENT, DERIVE_TESTS), plus REPLAN which is a PLAN variant introduced in M-G. Each skill is one OpenRouter call. Use the OpenRouter chat-completions API. Make the model configurable via env var `RICH_MODEL`; pin a default. Add retry-with-backoff and, on parse failure, dump the raw response (do not crash silently).

Each skill must produce **strictly parseable output**. Instruct the model to return only the specified JSON/format with no prose or markdown fences, and parse defensively (strip fences if present, retry on parse failure up to a small cap).

### 5.0 The root seed (you/the caller provide this, once)
The system is invoked as `build(root_contract)`. The `root_contract` is authored from the user's goal. For v1, the caller supplies the goal as a contract directly (or a thin `goal_to_contract` step that is itself PLAN-shaped). Do not over-engineer this; it is one contract at the top.

### 5.1 PLAN(contract) → decision
- **Input:** the contract this node must satisfy.
- **Output (JSON):** either
  - `{"is_leaf": true}` — if the contract is small enough to implement directly within budget; or
  - `{"is_leaf": false, "children": [<contract>, ...], "edges": [{"from": "<child_id>", "to": "<child_id>", "name": "<inject_param_name>"}, ...]}` — where each child `<contract>` is a full contract (§5.4 schema) **authored by this PLAN call**, and edges declare which children depend on which (by injection name).
- **This is the architect move.** Its decomposition output literally authors the children's contracts. This is the highest-risk call in the system. The children must form a DAG (validate; reject cycles).
- Stubbed (canned) until M-D.

### 5.2 IMPLEMENT(contract, dep_contracts, pipeline=False) → source
- **Input:** the contract to satisfy; the contracts of dependencies (empty for leaf; children for internal); `pipeline` flag.
- **Output:** source code (a single file for v1) implementing the contract. It must:
  - expose the operations named in `contract.interface` with matching signatures;
  - receive each dependency as a **named injected handle** per `dep_contracts` (D4) — never import dependencies;
  - for internal/pipeline nodes, compose the dependencies as a sequential pipeline (D9).
- **Prompt must include:** the contract, the dependency *contracts* (NOT their source — this is the firewall, D5), and on retry, the failing test output.
- Stubbed until M-C.

### 5.3 DERIVE_TESTS(contract) → tests
- **Input:** the contract.
- **Output:** an executable test file (pytest) that checks the contract's declared behavior — its operations' input/output expectations and enumerated error conditions. These are consumer-driven tests (D8).
- Tests run against the implementation in a subprocess (§6.1).
- Stubbed until M-C.

### 5.4 Contract schema (the object every skill reads/writes)
A contract is YAML with this shape. Keep it minimal for v1.

```yaml
id: <unique string>                 # also the node directory name
description: <one-line natural-language statement of what this module must provide>
interface:
  operations:
    - name: <op name>
      inputs:   { <param>: <type>, ... }     # types: string|int|float|bool|list<...>
      outputs:  { <param>: <type>, ... }
      errors:   [ <error name>, ... ]         # may be empty
dependencies:                                  # for v1, present on internal nodes; names match edges
  - name: <inject_param_name>
    id: <child id this name binds to>
behavior:                                      # consumer-authored, prose for v1 (formalizable later)
  - id: <stable prop id>
    prose: <what must be true>
```

Note: `behavior` is prose in v1. The `id` is stable so a later phase can attach formal/machine-checkable content without a rewrite. **Do not** build machine-checking of `behavior` in v1; DERIVE_TESTS turns `interface` (and prose `behavior` where it can) into tests.

---

## 6. The deterministic engines (build these to be rock-solid)

### 6.1 Verification = run consumer-derived tests
- `run_tests(src, tests) -> {passed: bool, failures: [...]}`.
- Execute the test file against the implementation in an isolated subprocess (pytest). Capture pass/fail and failure detail. Timeout-guard it.
- **Honesty requirement:** this is existential verification. A pass means "no violation observed on tested inputs," not a proof. Surface results plainly; never label a pass as "proven/verified-for-all-inputs."

### 6.2 Assembly = deterministic topological fold with injection
- `assemble(node) -> instance`:
  ```
  assemble(node):
      dep_instances = { name: assemble(dep_node) for (name, dep_node) in node.dependencies }
      return node.construct(**dep_instances)   # inject by name (D4)
  ```
- Instantiate leaves (no deps), inject upward in topological order. The **root's constructed instance is the deliverable.**
- Emit a generated entrypoint (`main.py`) that performs this fold over the final graph, so the deliverable is runnable standalone.
- **Shared dependency:** if a node appears as a dependency of multiple consumers (multiple in-edges), instantiate it **once** and inject the same instance into all consumers.
- **Diagnostic rule (D6/Trap 2):** if a dependency name in a consumer cannot be matched to a declared dependency, FAIL with a clear "underspecified contract: unmatched dependency `<name>` in `<node>`" message. Do **not** guess. This unmatched-name condition is the signal that an upstream contract is too loose.

### 6.3 The firewall is the prompt (no separate component)
There is no firewall module in v1. Enforcement is: when building IMPLEMENT's prompt, include only the node's own contract + its dependencies' *contracts*. Never include dependency source. Because IMPLEMENT is a single stateless call, this is airtight by construction. (If, much later, modules need multi-turn file access, a materialized-tree workspace becomes necessary — explicitly out of scope now, §9.)

---

## 7. On-disk layout (the node model)

Each node is a directory under a build root:

```
build/<id>/
  contract.yaml      # authored by parent (or root seed); §5.4
  decision.json      # {is_leaf:true} or {is_leaf:false, children:[...], edges:[...]}
  deps.yaml          # resolved dependencies: [{name, id}]  (internal nodes)
  src/               # implementation (one file for v1): leaf code or wiring
  tests/             # pytest file from DERIVE_TESTS
  status.json        # {status: planned|implemented|verified|failed, reason?}
build/main.py        # generated entrypoint (assembly fold) — produced at the end
```

Persist state at each transition so a run is inspectable and (from M-G) resumable/memoizable.

---

## 8. Milestones (build in this exact order; each leaves the system runnable)

**M-A — Skeleton + driver + canned skills (NO real LLM).**
Implement the node model (§7), the `build()` recursion (§4), status/decision persistence. Stub PLAN, IMPLEMENT, DERIVE_TESTS to return **hardcoded** results for ONE chosen example goal (pick a trivial pipeline, e.g. "normalize then validate a string"). Canned IMPLEMENT returns real (hardcoded) source; canned DERIVE_TESTS returns real (hardcoded) pytest.
- ✅ `build(root_contract)` for the canned example creates the full tree on disk, runs the (real) canned tests, marks nodes verified. **Zero LLM calls.** You are debugging control flow in isolation.

**M-B — Real verification + real assembly (still canned skills).**
Make §6.1 (`run_tests`) and §6.2 (`assemble` + generated `main.py`) fully real. From the canned modules of M-A, assemble a runnable `build/main.py` and execute it to produce the deliverable.
- ✅ The canned multi-module tree assembles via the topological fold, injecting deps by name, and `python build/main.py` runs and produces correct output. The entire deterministic back-half is now trustworthy independent of any agent.

**M-C — Real IMPLEMENT + DERIVE_TESTS, LEAF ONLY.**
Replace the IMPLEMENT and DERIVE_TESTS stubs with real OpenRouter calls (PLAN still stubbed to "leaf"). Wire model config, retry, raw-dump-on-parse-fail. Test on a single-leaf goal.
- ✅ Given a single-leaf contract, the system generates source + tests via LLM, runs the tests, and converges (within K_IMPL) to a verified leaf. This is the self-verifying atom.

**M-D — Real PLAN, leaf decisions only.**
Replace the PLAN stub with a real call **restricted to returning `is_leaf:true`** (decomposition still disabled — if PLAN wants to decompose, treat as leaf or error for now). Combined with M-C: goal → root contract → PLAN says leaf → implement → verify, fully autonomously for single-module goals.
- ✅ A single-module goal builds end-to-end with all three skills real, no human canning.

**M-E — Enable decomposition, DEPTH 1, NO fan-in. (THE MVP.)**
Allow PLAN to return `is_leaf:false` with children + edges. `build()` recurses one level; internal-node IMPLEMENT writes pipeline wiring against child contracts; assembly injects children. Children are independent (no shared deps). Choose a goal whose decomposition is a clean sequential pipeline (D9).
- ✅ A depth-1 goal decomposes into N independent children, each built+verified, the parent wires them as a pipeline, the tree assembles and `main.py` runs. **This is the minimum viable system: agent-decomposed, agent-implemented, verified, assembled, runnable.**

**M-F — First fan-in (shared dependency, depth 1).**
Use a goal where two children share a third child (A→C, B→C). The parent's PLAN authors A, B, and C's contracts and reconciles C to serve both A's and B's needs (this reconciliation is LOCAL to the parent — it authored all three). Assembly instantiates C once (§6.2 shared-dependency rule).
- ✅ A shared dependency is authored once by the common parent, built once, and injected (same instance) into both consumers; the deliverable runs. (This is the first real test of PLAN's cross-module judgment — expect to iterate on the PLAN prompt here.)

**M-G — Depth > 1 + backtracking + caps + memoization.**
Allow arbitrary recursion depth. Implement REPLAN (PLAN conditioned on a child's FAILURE + reason) and the backtracking path in §4. Add hard caps: max depth, max children-per-node, K_IMPL/K_WIRE, and a global LLM-call/spend ceiling — fail loudly when exceeded. Add memoization (verified node + unchanged contract ⇒ never rebuild).
- ✅ A multi-level goal builds; a deliberately-hard child that fails K times triggers a REPLAN at its parent and the system recovers or fails loudly within caps; an unchanged subtree is not rebuilt on a re-run.

**M-H — (Fork, only if M-A..G are solid) Hardening forks.**
Each of these is a separate, optional, gated extension. Do NOT start any until M-G is solid. Pick based on what real use demands:
- Shared **stateful** dependencies (the parent must recognize "these consumers share one *stateful* C" — mechanics already handled by §6.2; the new part is PLAN recognizing it and the state semantics being correct).
- Richer wiring beyond pipelines (conditional/error/rollback) — this reopens composition expressiveness; treat as a real design task, not a tweak.
- Formal/SMT verification (∀ instead of ∃) — requires giving the expression/AST layer a visitor structure; build that conversion in the same stroke.
- Cross-subtree deduplication of shared demand (two distant cousins both need "logging").
- Materialized-tree workspaces for modules too large for single-call implementation.

---

## 9. Non-goals for v1 (say no on purpose)

- **No filesystem firewall / sandbox / OS jail.** The prompt is the boundary (D5/§6.3).
- **No "compiler agent" or intelligent assembler.** Assembly is the deterministic fold (D6/§6.2). Assembly ambiguity = upstream contract bug, not a reason for intelligence.
- **No SMT / formal / machine-checked behavioral verification.** v1 verification is running consumer-derived tests (D8). `behavior` stays prose with stable ids.
- **No non-pipeline wiring.** No conditional routing, error-branching, partial rollback, or loops in internal-node composition (D9).
- **No global reconciliation component.** Fan-in is reconciled locally by the common-ancestor node that authored the shared contract (§4 invariants, M-F).
- **No self-hosting, no multi-language.** v1 targets a single implementation language (Python) for generated modules.
- **No rich type system.** Contract types are `string|int|float|bool|list<...>` only (§5.4).
- **No persistence/resumability beyond what M-G's memoization needs.**

---

## 10. The one risk to keep in view while building

**PLAN quality is the irreducible core risk, and its failures surface late** (deep in the recursion, after spend). There is no automated check that a decomposition is *good* (minimal, well-factored) — the DAG check, the budget, and build-success only ensure it is *valid* and *satisfiable*, not *good*. Backtracking recovers from decompositions that are *wrong* (a child can't be satisfied), not from ones that are merely *bad* (satisfiable but redundant or awkward). This is why the deterministic engines (§6) must be rock-solid and the skills (§5) isolated: when something goes wrong, you must be able to tell instantly whether it was the mechanical scaffolding (it shouldn't be, if M-A/M-B were done right) or PLAN's judgment (it usually will be). Build §6 so trustworthy that all suspicion correctly falls on the prompts.

---

## 11. First concrete action

Implement **M-A**: the node directory model (§7), the `build()` recursion (§4) with all three skills returning canned output for ONE hand-picked trivial pipeline goal, and persistence of `contract.yaml`/`decision.json`/`status.json`. Prove the control flow with zero LLM calls. Only then proceed to M-B (real verification + assembly), and only after that introduce the first real LLM call in M-C.
