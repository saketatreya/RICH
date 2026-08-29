# CLAUDE.md

Guidance for Claude Code (claude.ai/code) working in this repository.

## Commands

```bash
python -m pip install -e '.[test]'    # install (editable) with test deps
npm --prefix web ci                   # frontend deps
npm --prefix web run build            # frontend → web/dist, served by the Python server

# The offline suite — what CI runs. It never calls a model or a provider.
# Needs `bwrap` on PATH: the sandbox-argv tests resolve it while building a
# command (they never execute it), so a host without Bubblewrap fails 12 tests
# in tests/test_executor.py.
ruff check .
python -m pytest
python -m pytest tests/test_store.py                # one file
python -m pytest tests/test_store.py::test_name     # one test
npm --prefix web run typecheck

# Run it: one server, API and canvas on the same port.
rich serve                            # → http://127.0.0.1:8767
rich doctor                           # host checks
rich rebuild-node --project P --node domain   # forget one node's memo
rich cancel-run RUN_ID                        # stop at the next checkpoint
rich logs RUN_ID --follow                     # watch a run as a timeline
rich plan-change --project P --from-spec A --to-spec B \
  --from-architecture C --to-architecture D   # what an amendment costs
rich apply-change ...                         # stale exactly that set
npm --prefix web run dev              # hot-reload dev; Vite proxies /v1 → :8767

# Live tests (marker `live`, skipped unless --run-live is passed)
python -m pytest --run-live tests/test_executor.py
python -m pytest --run-live tests/test_typescript_obligations.py
python -m pytest --run-live tests/test_claude_code_provider.py
# ^ one small real `claude -p` call; needs a Claude Code login, no API key.
# ^ obligations run the generated sampler under the pinned Node and check every
#   drawn value against ValueType.accepts. Needs `node` 22.x; no model.
python -m pytest --run-live --basetemp=.rich/live-tests tests/test_public_runtime_live.py
# ^ downloads locked pnpm deps + Chromium (>2 GiB); needs a non-tmpfs basetemp
#   and Linux with Bubblewrap + user namespaces. It does NOT call a model.
python -m pytest --run-live --basetemp=.rich/live-loop tests/test_closed_loop_live.py
# ^ the whole loop: intent → architecture → scaffold → a REAL model authors
#   source → lint/typecheck/unit/properties/build/Playwright in Bubblewrap.
#   ~3.5 min and a few dollars of quota.
python -m pytest --run-live --basetemp=.rich/locality \
  tests/test_change_locality_live.py
# ^ build, amend a requirement one component does not serve, rebuild: that
#   component replays its memo while the ones that do serve it are rewritten,
#   and every gate runs again. ~9 min, two model-backed builds.
```

## What this repo is

**RICH is an intent-to-verified-software compiler.** One system, one package
(`src/richbuild/`), one server, one UI.

Interview → approved immutable spec revision → approved architecture → compiled
dependency-ordered tasks → frozen Next.js target-pack scaffold → bounded
model-authored source → independent sandboxed gates (lint / types / unit /
contract obligations / build / Playwright) → content-addressed release ZIP →
digest-bound Neon/Vercel preview. SQLite plus a SHA-256 content-addressed
artifact store (`store.py`) is the source of truth, not the filesystem.

The canvas (`web/`, React + Vite + React Flow) is the product surface: run the
interview, review and revise the architecture as a graph, approve each gate,
watch the run, read what each node produced, rebuild one node, deploy a preview.

Read `docs/architecture.md` before changing anything — it is the operating
contract.

## Non-negotiable invariants

- **Everything fails closed.** A transition proceeds only when identity,
  approval, ownership, budget, lease, sandbox, evidence, and digest checks all
  pass. Never add a permissive fallback: no unsandboxed execution path, no
  alternate model/provider fallback, no clipping a budget overage.
- **Model output is never evidence.** Only independently observed command
  results (trusted runner, Bubblewrap, network off) can publish task/run
  success, and the protected Playwright reporter must return the exact passed
  scenario-ID set bound to the run/task/attempt nonce. Evidence may flow the
  other way — a failed gate's redacted output informs the next attempt
  (`redact_diagnostics`) — but generation may never become evidence.
- **Approvals bind exact revisions.** Revisions are append-only; approving one
  never authorizes a later one. Gates are validated against `ApprovalGate` at
  the store boundary, so an approval cannot be opened at a gate nothing checks.
- **Generated source is confined to approved owned paths.** Package manifests,
  lockfiles, tests, configs, the pinned operations interface, and RICH metadata
  are protected generation inputs the model cannot touch.
- **Obligations are run, not just declared.** A contract's proof obligations are
  scaffolded as a vitest suite against `packages/contracts/src/operations.ts`
  and executed as a distinct PROPERTY gate; the domain node must export
  `operations` from `packages/domain/src/operations.ts`. No suite scaffolded
  means no gate — a property run over an empty directory would pass without
  checking anything.
- **A change costs what it changes.** `change.py` computes the stale set
  from two approved revisions. An implementation change cannot reach a
  consumer (the firewall); a contract change invalidates consumers
  transitively. Contracts are compared on behaviour, never on
  planner-defined metadata. Allocation decides the cost, so the architect
  is asked for the minimum one.
- **Generation is memoized; verification never is.** `generation_cache_key`
  hashes the exact request (both prompts, provider, model, response schema); a
  hit replays the bundle through the same parser, transaction and gates.
  `rich rebuild-node` forgets one node's memos.
- **One fenced owner mutates a run.** SQLite leases with fencing tokens checked
  in the same transaction as every authoritative write; source writes go through
  CAS-backed write-ahead transactions. Coding is single-worker by design.
- **Money is a decimal string, never a float.** Budgets must be complete;
  reservations are recorded before provider calls and settled after; crashes
  charge the reservation.
- **One canonical encoding.** `canonical.py` is the single definition of the
  bytes digests are taken over. Never add a second.
- **One path guard.** `paths.py` is the single relative-path validator and
  ownership check. `models/_common.py` keeps its own copy on purpose, with the
  same rules: a layering test says the `models` package is the bottom of the
  stack and imports no sibling module.
- Toolchain identity is exact (Node 22.22.3, pnpm 10.34.5) and the sole trusted
  model policy is `anthropic/claude-sonnet-5` — no silent fallback. Two
  **routes** reach it, chosen explicitly via `route=` on `default_run_runtime`
  and never substituted for one another: `"api"` (`anthropic_provider.py`, needs
  `ANTHROPIC_API_KEY`) and `"claude-code"` (`claude_code_provider.py`, spends an
  existing `claude` login). The CLI route runs `claude -p --tools ""` in an
  empty cwd under a throwaway `HOME` holding only a symlink to the credential —
  without that isolation the worker receives the operator's own `CLAUDE.md`
  memory, which the information firewall exists to prevent. It cannot bound
  output tokens before the fact. `openai_provider.py` is retained but wired to
  nothing; it keeps the `ModelProvider` seam vendor-neutral and must not become
  a fallback.
- API rules: `/v1` prefix, loopback bind, mutations require `Idempotency-Key`,
  bounded bodies, host/origin checks.

## Module map

`interview.py` / `compiler.py` / `planner.py` / `architect.py` (intent → spec →
architecture → tasks), `models/` (typed objects, six modules by subject, one
import name), `change.py` (what an amendment costs), `store.py` (SQLite + CAS),
`canonical.py`, `paths.py`, `fs.py`, `budget.py`, `scheduler.py` /
`run_engine.py` / `execution.py` / `executor.py` (fenced execution + Bubblewrap
gates), `coding.py` + `anthropic_provider.py` / `claude_code_provider.py` /
`providers.py` / `_http.py` (bounded generation), `target_packs/` (Next.js pack
+ TypeScript obligation compiler), `preview.py` (Neon/Vercel), `runlog.py` (a
run as a timeline), `control_plane.py` / `api.py` / `cli.py` / `runtime.py`
(surfaces).

## The program and the board

`docs/program.md` is the approved program: three releases, twenty-two milestones,
one customer scenario as the definition of done. `docs/board/cards/*.md` is the
tracker — one file per card — and `docs/board.html` is rendered from it, never
edited. Move a card when work starts, lands (with its commit), blocks, or a
finding changes the plan; then `python tools/board.py render`. `tests/test_board.py`
fails the suite when the board lies (a done card without a commit, a milestone
without a card). Run `python tools/board.py verify` before pushing a milestone.

Each milestone ends with its drive — a real browser against `rich serve`, under
`web/drive/` (`npm --prefix web run drive:m1`). A milestone that moves no step
of the customer scenario is not on the plan.

## Testing conventions

- Any test that calls an external model or provider carries the `live` marker
  (skipped by default via `tests/conftest.py`; enabled with `--run-live`) and
  self-skips with a useful message when its provider is unavailable. Never put
  credentials — even placeholders — in test source.
- Pytest is configured with `--strict-markers`, `xfail_strict`, and
  `pythonpath = ["src"]` (import `richbuild` directly; no install needed).
- Ruff lint scope is intentionally narrow (`E4`, `E7`, `E9`, `F`), target py310.

## Credentials

Resolved lazily from env, never persisted in run documents or model events:
`ANTHROPIC_API_KEY` (the `api` route; the `claude-code` route needs no key and
deliberately does not inherit one, so an expired login fails closed instead of
silently changing payer), `NEON_API_TOKEN` + `VERCEL_TOKEN` (previews). No
generated Node process ever receives the preview database credential —
migrations run through trusted Python/psycopg only.
