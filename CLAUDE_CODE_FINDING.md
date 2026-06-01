# CLAUDE_CODE_FINDING.md

**Investigation:** Can a Claude Code subagent replace the OpenRouter API as the model RICH calls internally (for PLAN / IMPLEMENT / DERIVE_TESTS), while leaving RICH's deterministic engine unchanged?

**Date:** 2026-05-31
**Status:** Path A taken (P1 = GREEN). See §3 for end-to-end results.

---

## 1. How RICH works (P0)

RICH builds software by **recursive decomposition with bounded LLM workers**. One recursive procedure (`build(contract)`) + three LLM "skills" + two deterministic engines. The recursion, DAG validation, backtracking, memoization, and assembly are all deterministic Python; only the three skills are non-deterministic, and today they call OpenRouter.

### 1.1 The three skills — exact signatures and output contracts

A subagent replacing the model must produce **exactly** these shapes. All three are defined in `skills.py` and all three obtain their result by calling `llm.call_with_retry(...)` then `llm.parse_json_response(raw)`.

| Skill | Python signature (`skills.py`) | LLM must return (JSON) | Skill returns to `build()` |
|---|---|---|---|
| **PLAN** | `plan(contract: dict, allow_decompose=False) -> dict` | `{"is_leaf": true}` **or** `{"is_leaf": false, "children": [<full child contracts>], "edges": [{"from","to","name"}]}` | the decision `dict` |
| **IMPLEMENT** | `implement(contract, dep_contracts=None, pipeline=False, prior_failures=None) -> str` | `{"source": "<python source as a string>"}` | `result["source"]` (a `str`) |
| **DERIVE_TESTS** | `derive_tests(contract: dict) -> str` | `{"tests": "<pytest source as a string>"}` | `result["tests"]` (a `str`) |

Notes that matter for the subagent:
- **PLAN** in `allow_decompose=False` mode is forced to `{"is_leaf": true}` regardless of model output (`skills.py:104`). In decompose mode the returned children must each be a full contract (`id`, `description`, `interface.operations`, `dependencies`, `behavior`) and form a DAG — validated by `_validate_dag` / `_validate_child_contracts`, which fall back to leaf on `ValueError`.
- **IMPLEMENT** system prompt demands: leaf mode → top-level functions named exactly as the operations, each returning a dict matching declared outputs; pipeline mode → a class whose `__init__` receives deps by name and composes them (never imports). The firewall (D5): the prompt contains the node's own contract + dependency *contracts only*, never dependency source.
- **DERIVE_TESTS** must emit a pytest file importing `from <module_id> import <op>` and asserting on the dict keys of declared outputs.

### 1.2 The exact model-invocation seam

The seam is a single function in `llm.py`:

```python
# llm.py
def call_with_retry(system_prompt, user_prompt, *, model=None,
                    temperature=0.1, max_tokens=4096, json_mode=True,
                    max_retries=3) -> str          # returns RAW model text
#   └─ call_llm(...) POSTs to OpenRouter and returns
#      result["choices"][0]["message"]["content"]   (a raw string)
```

Every skill in `skills.py` does exactly:
```python
raw = call_with_retry(system_prompt=SYSTEM, user_prompt=USER, temperature=…, max_tokens=…)
result = parse_json_response(raw, context="…")   # strips ```fences```, repairs escapes, dumps raw on failure
return result["source"]   # or ["tests"], or the whole decision dict
```

So **the entire seam is: a function that takes `(system_prompt, user_prompt)` and returns a raw text string** which the skills parse as JSON. `is_available()` (`= bool(API_KEY)`) gates whether real calls happen. To swap the backend, we only need to provide a `call_with_retry`-compatible function whose returned text `parse_json_response` can parse — **no change to `build.py`, `node.py`, the recursion, or assembly is required.**

### 1.3 Canned (no-model) baseline — confirmed passing

With `OPENROUTER_API_KEY` unset, the deterministic engine runs entirely on canned skill data. All four canned demos pass:

| Demo | Command | Result |
|---|---|---|
| Canned pipeline (normalize→validate) | `python build.py` | ✓ tree built, `main.py` runs, `valid=True, reason=OK` |
| Fan-in (shared `regex_engine`) | `python build.py --fan-in` | ✓ `construct_regex_engine()` called **exactly once** in `main.py` |
| Depth-2 recursion | `python build.py --deep` | ✓ `password_pipeline` has 2 grandchildren, all `verified` |
| Memoization | `python build.py --memo-test` | ✓ second build served from cache in ~0.01s |

This establishes the baseline that must not break: **the deterministic back-half works with zero model calls.**

---

## 2. Can a subagent be RICH's model? (P1)

### Verdict: **GREEN** ✅

A Python process *can* programmatically invoke a Claude Code worker with a given prompt and capture parseable structured output. The mechanism is the **`claude -p` (print / headless) CLI**, invoked via `subprocess`.

### 2.1 The exact mechanism

`claude -p "<prompt>"` runs Claude Code non-interactively, prints the response, and exits. The flags that make it a drop-in for `call_llm`:

| Flag | Role in the substitution |
|---|---|
| `--append-system-prompt <s>` | carries the skill's system prompt (PLAN/IMPLEMENT/DERIVE_TESTS) |
| `<user_prompt>` (positional) | carries the contract / dep-contracts / prior-failures |
| `--output-format json` | returns a telemetry **envelope**; the model's text is in `.result` — the exact analogue of OpenRouter's `choices[0].message.content` |
| `--model haiku\|sonnet\|opus` | model selection (analogue of `RICH_MODEL`) |
| `--disallowedTools Read Bash Glob Grep Edit Write WebFetch …` | **the firewall**: makes the worker a pure text generator that *cannot* read sibling source |
| `--max-turns 1` | bounds the worker to a single turn (no autonomous wandering) |

Authentication is inherited automatically from the host (keychain/OAuth) — no API key needed; the plain probe worked with `OPENROUTER_API_KEY` unset.

> **Framing note (honest):** `claude -p` spawns a *fresh headless Claude Code session*, not a "subagent" in the in-session Task-tool sense. The in-session Task/Agent tool can only be called from *within* an interactive session — it is **not** reachable from an arbitrary external Python script. For RICH (a Python process that needs a bounded worker as a subroutine), `claude -p` is the correct and only externally-callable mechanism, and it is functionally exactly the bounded worker RICH needs. The substitution is real; only the label "subagent" is loose.

### 2.2 Working probe (quoted)

A Python process spawning the worker, constraining it, and parsing its output through **RICH's own** `parse_json_response`:

```python
cmd = ["claude", "-p", "--model", "haiku", "--output-format", "json",
       "--append-system-prompt", system_prompt,
       "--disallowedTools", "Read","Bash","Glob","Grep","Edit","Write","WebFetch","WebSearch","Task",
       "--max-turns", "1", user_prompt]
proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
envelope = json.loads(proc.stdout)
raw_text = envelope["result"]           # == OpenRouter's message.content
parsed = parse_json_response(raw_text)  # RICH's existing fence-stripper
```

Output captured for an IMPLEMENT-shaped request (`{"source": ...}`):
```
RAW (.result field):
'```json\n{"source": "def add(a: int, b: int) -> dict:\\n    return {\\"result\\": a + b}"}\n```'
TELEMETRY: {'wall_s': 4.72, 'cost_usd': 0.0285, 'duration_ms': 3892, 'is_error': False}
PARSED keys: ['source']
>>> P1 PROBE PASS: Python spawned subagent, got parseable structured output.
```

### 2.3 Output-parseability

The worker is **chatty exactly as the spec warned** — it wraps JSON in ` ```json … ``` ` fences despite instructions. This is a *non-issue* because RICH's existing `parse_json_response` (`llm.py:130-137`) already strips fences and repairs invalid escapes, then dumps raw on failure. The subagent's output is *more* fence-prone than a raw completions call, so the adapter hardens this further (see §3), but the existing defense already handled every probe.

### 2.4 Firewall-constrainability — tight

Proven with a negative control:
- **Tools disabled** (`--disallowedTools Read Bash …`): asked to read `sibling_source.py` → returned `CANNOT_ACCESS_FILES`. It physically could not.
- **Tools allowed** (`--permission-mode bypassPermissions`): same prompt → it read the file and reported the secret.

So tool-disabling genuinely enforces the boundary. This is **stronger** than RICH's v1 firewall (D5: "the prompt is the boundary" — RICH doesn't sandbox at all, it just omits source from the prompt). With a tool-less worker we get prompt-boundary *and* no filesystem access.

### 2.5 Latency & cost (practicality)

Per trivial call, `--model haiku`: **~2.7–4.7s wall**, **~$0.011–$0.028**. The cost is dominated not by the task but by **Claude Code's own system prompt** (~24.5k cached input tokens ride along on every invocation — visible as `cache_read_input_tokens` in the envelope). Implications for RICH:
- Each skill call is heavier than a raw OpenRouter completion (process spawn + CC system-prompt overhead).
- A leaf build = PLAN + DERIVE_TESTS + up to 3×IMPLEMENT ≈ 5 calls ≈ 15–40s. A small decomposition (root + N children) is minutes. Deep recursion (dozens of calls) is practical for demos but the per-call floor (~3s + CC overhead) makes large trees slow and not free. Quality skills (PLAN/IMPLEMENT) will want `sonnet`, which is slower/pricier than the haiku figures above.

**Conclusion:** GREEN — proceed to Path A (subagent as drop-in model). The adaptation touches only the model backend, exactly as hoped.

---

## 3. Path A — results (P2 / P3 / P4)

The backend is `subagent_skill.py` (new file). It exposes a `call_with_retry()` with the
exact signature `skills.py` expects, implemented via `claude -p`, and an `install()` that
monkeypatches the seam (`call_with_retry`, `is_available`, `parse_json_response`) in `skills.py`
— the same boundary-swap pattern `test_harness.py` uses. **No change to `build.py`, `node.py`,
the recursion, or assembly.** It also adds a hardened, reused parse defense (§3.4).

### 3.1 GATE P2 — three skills route through a subagent ✅ PASS

Direct calls to each skill via the backend (`/tmp/p2_gate.py`):

| Check | Result |
|---|---|
| PLAN returns `dict` with `is_leaf` | ✅ `is_leaf=True` on a trivial contract |
| IMPLEMENT returns non-empty source `str` | ✅ |
| IMPLEMENT composes dep by injected name, no `import` of dep | ✅ produced `class GreeterPipeline: __init__(self, formatter); … self.formatter.format(name)` |
| DERIVE_TESTS returns pytest `str` | ✅ imports module, asserts on output dict keys |
| **Firewall** | ✅ dep context is contract-only; worker has no file tools (proven in §2.4) |

3 calls, $0.107, avg 5.5s/call (sonnet).

### 3.2 GATE P3 — one real leaf, end to end (the atom) ✅ PASS — through *unmodified* `build()`

`build.build(slugify_contract, allow_decompose=False)` with the subagent backend installed
(`/tmp/p3_leaf.py`):

- PLAN → leaf; DERIVE_TESTS → 8-case pytest; IMPLEMENT → source; `run_tests` → **passed on attempt 1**.
- Node reached **`status=verified`**, `is_leaf=True`. The generated tests ran against the generated source.
- Generated source:
  ```python
  import re
  def slugify(text: str) -> dict:
      text = text.strip().lower()
      text = re.sub(r'[^a-z0-9]+', '-', text)
      text = text.strip('-')
      return {"slug": text}
  ```
- 3 calls, $0.13, 44s. **IMPLEMENT attempts: 1.**

**This is the headline atom: the substitution works through RICH's unmodified engine.**

### 3.3 GATE P4 — one real decomposition (scaling past a leaf) — substitution ✅, with a documented RICH-core caveat

`build.build(process_text_contract, allow_decompose=True)` (`/tmp/p4_build.py`).

**The decomposition PLAN chose** (sensible — a clean 3-stage pipeline):
```json
is_leaf: false
children: [strip_html, collapse_ws, truncate]
edges: [ {from: strip_html,  to: collapse_ws, name: text},
         {from: collapse_ws, to: truncate,    name: text} ]
```
(PLAN initially returned `is_leaf:true` for a terse goal; reframing the seed to call for three
independent, separately-testable modules — legitimate root-seed authoring per spec §5.0 —
produced the decomposition above. Child ids matched the seed's declared deps, so `build()` wired them.)

**What the subagent did correctly (every skill):**
- ✅ PLAN decomposed into 3 sensible children with correct pipeline edges.
- ✅ **All 3 children built and reached `verified`** via subagent IMPLEMENT+DERIVE_TESTS:
  `strip_html`→`re.sub(r'<[^>]*>','',text)`, `collapse_ws`→`re.sub(r'\s+',' ',text).strip()`, `truncate`→`text[:200]`.
- ✅ The root **wiring IMPLEMENT was correct**: a class composing the children in order:
  ```python
  def process(self, raw):
      r1 = self.strip_html.strip(text=raw)
      r2 = self.collapse_ws.collapse(text=r1["text"])
      r3 = self.truncate.truncate(text=r2["text"])
      return {"result": r3["text"]}
  ```
- ✅ **The decomposed deliverable runs correctly end-to-end** — composing the verified artifacts on
  sample inputs (`/tmp/p4_deliverable_demo.py`) passes all cases, e.g.
  `process("<html><body><h1>Title</h1><p>Some   body   text.</p></body></html>") → "TitleSome body text."`,
  `process("<p>"+"x"*250+"</p>") → "x"*200`.

**What did NOT pass through *unmodified* RICH — and why it is a RICH-core issue, not a subagent issue:**
The root node failed self-verification (`wiring failed after 3 attempts`) on a pytest **collection error**.
Cause: RICH's `DERIVE_TESTS` is **leaf-oriented** — its system prompt says *"import the module by name
(`from <module_id> import <op_name>`)"*, and `build.py:483` calls it for internal nodes with **only the
bare contract** (no dep contracts, no pipeline flag). So it generated `from process_text import process`
(a top-level function) while IMPLEMENT (correctly, per *its* prompt) produced a **class** `ProcessText`
with injected deps → import fails → collection error.

This is **mutually-inconsistent core skill prompts for internal nodes**, identical for any model backend
(OpenRouter would fail the same way). **Isolation proof** (`/tmp/p4_derivetests_isolate.py`): when the
subagent is given the dep contracts + a pipeline note (i.e. what a *fixed* RICH would send), it generates a
proper class-based test (with fake deps) that **passes 20/20** against the real verified wiring. So the
subagent is fully capable; the blocker is RICH's prompt.

A second, independent RICH-core limitation also stands between "verified tree" and "runnable `main.py`":
`assemble()` hardcodes real wiring only for `pipeline_demo.run` (`build.py:226`); every other internal op
gets `pass  # TODO: wire from contract` (`build.py:233`), so a generic decomposed `main.py` returns `None`.
Again model-independent.

**P4 verdict:** the *model substitution* succeeds completely (PLAN decomposes, all children verify, wiring
is correct and the deliverable provably runs). The decomposition does **not** fully self-verify/auto-assemble
through *unmodified* RICH because of **two pre-existing RICH-core gaps** (internal-node `DERIVE_TESTS`;
`assemble()` hardcoding) — neither caused by, nor fixable in, the model backend within this investigation's
no-core-changes rule.

### 3.4 Parse defense (reused + hardened)

The backend reuses RICH's `parse_json_response` for its dump-on-failure behavior but parses robustly first,
because subagent output is chattier and embeds real code:
- **Leading prose before fences** — observed (`"…a textbook decomposition:" ```json {…}```). Handled by a
  balanced-brace extractor.
- **Backslashes in code** — RICH's escape-"repair" regex (`llm.py:137`) **corrupts valid JSON**: it turns a
  valid `\\s` into an invalid `\\\s`, crashing every IMPLEMENT whose source has a regex. The backend fixes
  this by trying `json.loads` on the clean block **first**, then a *correct* escape-fixer that preserves
  already-escaped pairs. Unit-tested on the exact crash payload + prose/fence + lone-backslash cases.

---

## 4. Bugs / surprises

1. **RICH `llm.parse_json_response` corrupts valid JSON containing escaped backslashes** (`llm.py:137`).
   `re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', text)` rewrites a valid `\\s` → invalid `\\\s`.
   Trigger: any IMPLEMENT output with a regex (e.g. `re.sub(r'\s+', …)`). Crashed P4's first run with
   `Invalid \escape`. **Worked around in the backend** (correct escape-fixer); not fixed in core.
   *This latent core bug would also bite OpenRouter whenever its JSON contains `\\`.*
2. **Internal-node `DERIVE_TESTS` is leaf-only** (`build.py:483` + `DERIVE_TESTS_SYSTEM`): it gets only the
   bare contract and emits `from <id> import <op>`, incompatible with the dependency-injected **class**
   that IMPLEMENT produces for the same node. This is why P4's root can't self-verify. Core gap; not fixed.
3. **`assemble()` hardcodes only `pipeline_demo`** (`build.py:226/233`): generic decomposed roots get a
   stubbed op → `main.py` returns `None`. Core gap; not fixed.
4. **PLAN's leaf/decompose call is sensitive to seed framing** — a terse goal was judged a leaf; an explicit
   "three independent modules" framing produced the decomposition. Expected (spec §10 names PLAN the core risk).
5. **`claude -p` is chatty** (fences, occasional preamble) — fully absorbed by the hardened parser; never a failure after §3.4.

No RICH core file was modified. All fixes live in `subagent_skill.py`. (Edits limited to: new file
`subagent_skill.py`; new deliverable `CLAUDE_CODE_FINDING.md`. `build.py`/`node.py`/`skills.py`/`llm.py` untouched.)

---

## 5. Cost / latency

Per-call telemetry (from the `--output-format json` envelope; `sonnet` unless noted):

| Call type | Wall | Cost |
|---|---|---|
| trivial probe (`haiku`) | 2.7–4.7s | $0.011–$0.028 |
| PLAN (leaf decision) | ~3s | ~$0.03 |
| PLAN (decompose, 3 children) | ~19s | ~$0.04 |
| IMPLEMENT (leaf) | ~10s | ~$0.03 |
| DERIVE_TESTS (internal, large) | ~53s | ~$0.10 |
| **one leaf, end to end (P3)** | **44s** | **$0.13** |
| **3-child decomposition build (P4, to wiring)** | **~135s** | **$0.45** |

Each invocation carries a fixed floor: a process spawn **plus Claude Code's own system prompt**
(~24.5k cached input tokens ride along on every call — visible as `cache_read_input_tokens`).
**Practicality:** fine for leaves and small/medium trees (a depth-1 decomposition is minutes and
cents). Deep recursion with dozens of calls is workable but slow and not free — the per-call floor
dominates. Levers: use `haiku` for cheaper skills, `--bare` to shed CC overhead (requires
`ANTHROPIC_API_KEY` auth, not the keychain/OAuth this environment used), and RICH's existing
memoization to avoid rebuilds.

---

## 6. Bottom line

**Yes — a Claude Code subagent (via `claude -p`) is a viable drop-in replacement for the OpenRouter
model in RICH.** A Python process can spawn it, constrain it tighter than RICH's own firewall, and
capture parseable structured output; the seam is a single `call_with_retry`-shaped function, so the
deterministic recursion/assembly stay untouched. Proven: the leaf atom (P3) builds and verifies through
RICH's **unmodified** `build()`, and a real decomposition (P4) sees PLAN choose a sensible 3-stage split
with every child verified and the wiring correct and provably runnable end-to-end.

**Minimal change to make it permanent:** ship `subagent_skill.py` and call `subagent_skill.install()`
at startup (or add a `RICH_BACKEND=subagent` flag in `skills.py` that imports it). Keep the hardened
parser. That's the whole substitution — zero changes to the engine.

**What's NOT done (and isn't a subagent problem):** a fully self-verifying, auto-assembled *decomposition*
is blocked by two pre-existing RICH-core gaps — internal-node `DERIVE_TESTS` is leaf-only, and `assemble()`
hardcodes `pipeline_demo`. Both are model-independent (OpenRouter hits them too) and are exactly the
follow-on hardening the spec scopes out of this investigation. The isolation probe shows the subagent
handles internal-node tests correctly the moment RICH passes it the dependency context — so fixing RICH's
two core gaps (not the model) is all that stands between this result and end-to-end decomposed deliverables.
