# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
python -m pip install -e '.[test]'   # install (editable) with test deps

# Offline suite — this is what CI runs; it never calls a model or provider
ruff check .
python -m pytest
python -m pytest tests/test_v2_store.py                    # one file
python -m pytest tests/test_v2_store.py::test_name         # one test

# Live tests (marker `live`, skipped unless --run-live is passed)
python -m pytest --run-live tests/test_v2_executor.py
python -m pytest --run-live --basetemp=.rich/live-tests tests/test_v2_public_runtime_live.py
# ^ downloads locked pnpm deps + Chromium (>2 GiB); needs a non-tmpfs basetemp and
#   Linux with Bubblewrap + user namespaces. It does NOT call a model.

# v1 canned regression demos (no LLM, deterministic — keep these green)
python cli.py                # pipeline demo
python cli.py --fan-in       # shared-dependency fan-in
python cli.py --deep         # depth-2 tree
python cli.py --memo-test    # memoization/resume

# Canvas (web UI + JSON API on http://127.0.0.1:8765, v2 API under /v2)
npm --prefix web ci
npm --prefix web run build       # frontend → web/dist, served by canvas.py
npm --prefix web run typecheck
python canvas.py
npm --prefix web run dev         # hot-reload dev; Vite proxies /api → :8765

# v2 CLI
python -m rich_v2.cli doctor     # host checks (also installed as `rich-v2`)
```

`tests/run_tests.py` is a standalone, destructive, OpenRouter-backed phase runner that writes into `build/` — it is deliberately not part of the pytest suite.

## What this repo is

Two related systems share this repo:

- **v1 (root-level `.py` modules)** — a recursive LLM build engine: one procedure `build(contract)` either implements a module as a leaf or asks a PLAN skill to decompose it into children whose contracts the parent authors, then recurses. Three LLM skills (PLAN / IMPLEMENT / DERIVE_TESTS in `skills.py`) do the thinking; two deterministic engines in `build.py` (`run_tests` pytest-subprocess verification, `assemble` topological injection fold → `build/main.py`) do the rest.
- **v2 (`src/rich_v2/`)** — the active direction: an intent-to-verified-software compiler. Interview → approved immutable spec revision → approved architecture → compiled dependency-ordered tasks → frozen Next.js target-pack scaffold → bounded model-authored source → independent sandboxed gates (lint/types/unit/build/Playwright) → content-addressed release ZIP → digest-bound Neon/Vercel preview. SQLite + a SHA-256 content-addressed artifact store (`store.py`) is the source of truth, not the filesystem.

The Canvas (`canvas.py` + `web/`, React + Vite + React Flow) is the product surface for both: design a module graph, build it, inspect generated code/tests per module, rebuild one module (`build.invalidate_node`), preview vibe-edit diffs.

## v1 architecture (root modules)

- **The information firewall**: a module's IMPLEMENT call sees its own contract and its dependencies' *contracts*, never their source. The firewall is the prompt — do not leak dependency source into skill prompts.
- Contracts flow **down** from the parent (PLAN's decomposition output *is* the children's contracts). Dependencies are injected by constructor parameter, never imported; `assemble` does the wiring and constructs a shared dependency exactly once.
- Hard caps live in `build.py`: `K_IMPL=3`, `MAX_DEPTH=3`, `MAX_CHILDREN=8`, `MAX_LLM_CALLS=50`, `REPLANS_MAX=2`. There is intentionally no implement-then-check size enforcer — leaf-vs-decompose is PLAN's judgment.
- **Resumption is first-class**: re-running the same build memo-hits verified subtrees and reuses prior PLAN decisions by contract hash. `build/manifest.jsonl` is the append-only audit/cost ledger (`live-PLAN`, `memo-hit`, `decision-reuse`, …); for live gates `manual-plan-reuse` is a forbidden event. Per-call token/cost logging goes to `build/llm_calls.jsonl`.
- Backends are selected by `RICH_BACKEND=claude|codex|openrouter` (`backend.py`; default claude). `backend.install_from_env()` monkeypatches the skills. Claude mode runs `claude -p --tools "" --max-turns 6` subprocesses (`subagent_skill.py`); codex mode runs `codex exec` read-only in an empty temp dir (`codex_skill.py`); openrouter uses `llm.py`.
- `node.py` is the `Node` dataclass + on-disk persistence under `build/`; `tree_viewer.py` is strictly read-only over `build/`.

## v2 architecture (`src/rich_v2/`)

Read `docs/v2-architecture.md` before changing v2 — it is the operating contract. The non-negotiable invariants:

- **Everything fails closed.** A transition proceeds only when identity, approval, ownership, budget, lease, sandbox, evidence, and digest checks all pass. Never add a permissive fallback (no unsandboxed execution path, no alternate model/provider fallback, no clipping a budget overage).
- **Model output is never evidence.** Only independently observed command results (via the trusted runner, in Bubblewrap, network off) can publish task/run success, and the protected Playwright reporter must return the exact passed scenario-ID set bound to run/task/attempt nonce.
- **Approvals bind exact revisions**; revisions are append-only; approving one revision never authorizes a later one.
- **Generated source is confined to approved owned paths.** Package manifests, lockfiles, tests, configs, and RICH metadata are protected generation inputs the model cannot touch.
- **One fenced owner mutates a run**: SQLite leases with fencing tokens checked in the same transaction as every authoritative write; source writes go through CAS-backed write-ahead transactions. Coding is single-worker by design — parallelism requires worktrees + reverification, not racing one directory.
- **Money is a decimal string, never a float.** Budgets must be complete; reservations are recorded before provider calls and settled after; crashes charge the reservation.
- Toolchain identity is exact (Node 22.22.3, pnpm 10.34.5) and the sole trusted model policy is `anthropic/claude-sonnet-5` via `anthropic_provider.py` — no silent fallback. `openai_provider.py` is retained but wired to nothing; it exists to keep the `ModelProvider` seam vendor-neutral, and must not become a fallback.
- API rules: `/v2`, loopback bind, mutations require `Idempotency-Key`, bounded bodies, host/origin checks.

Rough module map: `interview.py`/`compiler.py`/`planner.py` (intent → spec → architecture → tasks), `models.py` (typed objects), `store.py` (SQLite + CAS), `budget.py`, `scheduler.py`/`run_engine.py`/`execution.py`/`executor.py` (fenced execution + Bubblewrap gates), `coding.py` + `anthropic_provider.py`/`providers.py` (bounded generation), `target_packs/` (Next.js pack), `preview.py`/`migration.py` (Neon/Vercel), `control_plane.py`/`api.py`/`cli.py` (surfaces).

## Testing conventions

- Any test that calls an external model or provider must carry the `live` marker (skipped by default via `tests/conftest.py`; enabled with `--run-live`) and must self-skip with a useful message when its provider is unavailable. Never put credentials — even placeholders — in test source.
- Pytest is configured with `--strict-markers`, `xfail_strict`, and `pythonpath = [".", "src"]` (import `rich_v2` directly, no install needed for tests).
- Ruff lint scope is intentionally narrow (`E4`, `E7`, `E9`, `F`), target py310.

## Credentials

Resolved lazily from env, never persisted in run documents or model events: `ANTHROPIC_API_KEY` (v2 model runs), `NEON_API_TOKEN` + `VERCEL_TOKEN` (previews), `OPENROUTER_API_KEY` (v1 openrouter backend). No generated Node process ever receives the preview database credential — migrations run through trusted Python/psycopg only.
