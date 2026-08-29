# RICH → a finished product

> The program approved on 29 Aug 2026. `v1.0.0` was already tagged on a tree a wheel
> cannot install, so the program's releases are numbered 2.0, 2.1 and 2.2. The live
> tracker is `docs/board.html`, rendered from `docs/board/cards/`.

## Context

RICH is an intent-to-verified-software compiler. After 135 commits the **kernel is
done and proven**: SQLite + CAS store, fenced leases, decimal budgets with
crash-conservative recovery, Bubblewrap sandboxing with no fallback, six independent
gates, evidence that model output can never publish, generation memoization, and change
compilation proven live (`domain` replayed, `web` rewritten, every gate re-ran). Zero
TODO/FIXME markers. No import cycles. Model layering enforced by a test.

A survey of the product as a customer would meet it says the rest is not done:

- **Intent entry is a developer's job.** `interview.py` is a form validator with no model
  call; the "adaptiveness" is four regexes. The customer hand-writes Playwright oracle
  steps as raw JSON in a textarea (`Editors.tsx:170-178`), invents stable ids, and
  cross-references scenarios by comma-separated id strings. 12 of the 17 steps from
  "open the app" to "deployed preview" require code, JSON, locators, ids, digests, npm
  scopes or provider ids.
- **The architect's tool is read-only.** `ArchitectureGraph.tsx:183-185` disables
  dragging and connecting; the only correction channel is a free-text redraft.
- **Iteration is wired to nothing.** `ChangeCost` compares every revision with itself
  (`ControlPlane.tsx:1025-1030`); no revision picker exists; loading an existing project
  nulls its spec/architecture/run (`ControlPlane.tsx:237-240`) and strands it.
- **The envelope is a brochure.** Every generated route is a GET; nothing writes.
  `packages/db` is scaffolded but an island: no dependency edge (frozen lock), no
  `DATABASE_URL` in any gate, no network. No auth, no integrations.
- **Components are horizontal layers**, so every feature cuts through all of them and
  change locality depends on the architect allocating minimally.
- **Money is invisible.** `settled_usage` sits in persisted events; nothing sums it.
- **Getting the software out is broken.** The canvas preview request sends the
  workspace-relative form string instead of `scaffold.destination` and fails every time;
  no release-ZIP download; no push to a repository; nothing beyond a preview.
- **Shipping is broken.** `pyproject.toml:29` lists packages explicitly and omits
  `richbuild.models`, so a wheel cannot `import richbuild`. The canvas is not bundled.
  No Dockerfile, no LICENSE, no `--version`. `docs/spec.md` describes a deleted system.
- **One security inconsistency.** `_FORBIDDEN_MODEL_EVIDENCE` (`run_engine.py:861-869`)
  omits `PROPERTY` while its sibling list (`:211-218`) has all six.

**Decisions taken (with the owner):**
1. **Local-first, packaged.** `pip install` + a Docker image. Customers bring their own
   Anthropic key or `claude` login. A hosted service is the one thing outside this
   program.
2. **Apps persist data, sign users in, and call external services.**
3. **Two tracks in parallel** — engine in isolated worktrees, product in this session;
   a third track for packs once the seam exists — merged, verified and driven end to
   end per milestone.
4. **This is a paradigm, not a web-app tool.** Any software should decompose this way.
   The program therefore ends with many packs, proof-tier obligations, parallel
   verified builds, richer contract semantics, and production promotion — not a
   polished web-app builder. Nothing on the original roadmap is out of scope; it is
   ordered.

**Intended outcome:** a product lead with no development background installs RICH,
describes software in prose, approves a readable spec, shapes the architecture as a
graph of capabilities, builds under a dollar budget, watches it be proven, promotes it
to production, and comes back next week to amend it and pay only for what changed —
without ever seeing JSON, an id, a digest, a locator or a package scope. The same
discipline compiles a Python service, a Rust binary, a data pipeline, infrastructure,
and a Lean program whose contract obligations are *proved*, not sampled.

---

## Definition of done: the customer drive

One fixed scenario is the acceptance test of the product. It is re-run at the end of
every milestone (the parts that exist) and in full before each release. It is the
antidote to "code for the sake of code": a milestone that does not move a step of this
drive is not on the plan.

> **Maya**, product lead, macOS laptop, no terminal habits.
> 1. `docker run` one line from the README. Opens `localhost:8767`. `doctor` is green.
> 2. Types: *"A task tracker for my team of eight. People sign in, create tasks with a
>    title, a due date and a priority, assign them, mark them done. Slack me when
>    something is overdue."* RICH asks three questions (who can see what, what
>    "overdue" means, what happens if Slack is down). She answers in prose.
> 3. Sees a specification as readable requirements and scenarios — *"Given a signed-in
>    member, when they create 'Ship v1' due Friday, then it appears in the list"* — each
>    with its executable steps rendered as sentences she can edit with dropdowns. Approves.
> 4. Sees the architecture as a graph of **capabilities**: identity, tasks,
>    notifications, web shell. Drags the "overdue" requirement from `tasks` into
>    `notifications`. Asks the architect to fill in `notifications`' contract. Reads it
>    as behaviour: *a task moves open → done → archived and never backwards* is a claim
>    she can see. Approves.
> 5. Clicks **Build**, sets $15. Four components build **at once**; nodes light up on the
>    graph, a cost meter climbs, events read in plain language. `notifications` fails
>    its property gate once; she reads *why* in a sentence; it retries and passes.
> 6. Assurance: every requirement proven, by which gates — including *"archiving an
>    open task is refused"* and *"creating a task then listing shows it"*. **Preview**
>    → a URL. Signs in, creates a task, reloads — it is still there. Slack is proven
>    against recorded fixtures; the preview calls real Slack.
> 7. **Promote to production.** A rollback point is taken, migrations apply, the exact
>    verified snapshot goes live, health passes. Downloads the ZIP; pushes to GitHub.
> 8. A week later: *"tasks can be snoozed."* Cost: 2 of 5 components. Rebuild. Every
>    gate re-runs. Promote. A bad migration in a later change **rolls back on its own**.
>    Never a JSON, id, digest or locator in sight.

> **Paradigm checks:** the same approved contracts, headless, compile through the
> Python service pack; a numeric core compiles through the Lean pack with its
> `PROOF`-tier obligations discharged by the kernel; a CLI through the Rust pack; a
> pipeline through the data pack; its infrastructure through the infra pack.

---

## How the work is done

1. **Drive it as a user at the end of every milestone.** The single most productive act
   this project has seen was driving it: four defects in an hour. Every milestone ends
   with its drive steps executed and a board card saying so.
2. **Git is the lab notebook.** Branch per milestone (`product/<m>`, `engine/<m>`,
   `pack/<name>`); commit per verified increment; measurements in the commit message;
   revert freely. Main is always green (`ruff`, `pytest`, `typecheck`, `build`).
3. **The board is the truth, generated not typed.** `docs/board.html` health strip is
   produced by `tools/board_health.py` from real numbers (test count, HEAD, tree state)
   so it cannot drift again. Cards move when work moves.
4. **Invariants are non-negotiable.** Each milestone names which it touches and how it
   preserves them. No permissive fallback, ever. `docs/architecture.md` is updated in
   the same commit that changes what it describes.
5. **Tracks, one main.** Engine and pack tracks work in `git worktree`s and land by
   fast-forward after green + drive. Conflict hot-spots are `api.py`,
   `control_plane.py`, `api.ts`: engine adds routes in *new* modules; product owns
   `api.ts`; packs own `target_packs/<name>.py` and touch the core only through the
   `TargetPack` protocol. Merge per milestone, never per day.
6. **What is not done:** no engine feature without a drive step it moves; no prompt
   change without a measured before/after; no third way to do a thing.

---

## The program: three releases

| Release | What a customer gets | Milestones |
|---|---|---|
| **2.0 · It builds real software** | prose → capabilities graph → persistence, sign-in, integrations → proven → preview → ZIP/repo → amend and pay for the change; the seam proven by a second pack | M0–M4, M7, M13, M8, M9, M5, M6, M10, M11, M12 |
| **2.1 · It proves more and ships to production** | parallel verified builds; sequence, state-machine, atomicity and concurrency claims proven; promotion with rollback | M14, M15, M16 |
| **2.2 · Any software** | TypeScript library, Lean (proof-tier), Rust, data/ML, infrastructure, mobile packs | M17–M22 |

Track **P** = product (this session). Track **E** = engine (worktree subagents).
Track **K** = packs (one worktree per pack, once M10's protocol exists).
Size: S ≈ half a day, M ≈ 1–2, L ≈ 3–4, XL ≈ 5+ agent-days.

---

## Release 2.0 — it builds real software

### M0 · Truth and hygiene — both tracks, first — size M

Fix everything known to be wrong before building on it.

**Engine (E)**
- `run_engine.py:861-869` add `EvidenceKind.PROPERTY` to `_FORBIDDEN_MODEL_EVIDENCE`;
  fix the message at `:220-221`. Test: a model result carrying non-blocking PROPERTY
  evidence is refused.
- `pyproject.toml:28-33` → `[tool.setuptools.packages.find] where = ["src"]`. CI job:
  build a wheel, install it in a clean venv, `python -c "import richbuild"`. Makes the
  defect unrepeatable.
- One canonical encoding: `providers.canonical_request_bytes` (`providers.py:620`,
  no trailing newline) vs `canonical.canonical_json_bytes`. **Before unifying, list
  what persists those bytes** (idempotency claims, reservation digests, memo keys);
  changing them invalidates existing memos — acceptable pre-2.0 if deliberate and stated.
- Duplicates → one definition: `_all_events` (`execution.py:215`/`run_engine.py:1806`),
  `_fsync_directory` (`coding.py:928`/`run_engine.py:2380`), vestigial `_is_owned`
  pass-throughs, `_bounded_read` + `_UrllibTransport` into a shared `_http.py` used by
  both providers, `_optional_string` (same name, opposite contracts in `api.py:948` and
  `control_plane.py:1135` — rename the coercing one).
- Delete the dead workflow block `compiler.py:734-1019` and its tests; make
  `target_packs/__init__.py` the one import path or delete it.
- `models/_common.py:549-564` path guard: add the null-byte, trailing-slash and 255-byte
  rules `paths.py:38-80` has, so "same rules" is true.
- `RunEngineConfig.max_task_attempts` 2 → 3 with the rationale in the docstring (the
  first live build needed a third attempt to act on property-gate output). Board card.
- `ScaffoldManifest.target_pack_version` default `"1.2.0"` at `nextjs.py:187` drifted
  from `:774`; one constant.
- `tests/conftest.py`: shared fixtures (`store`, `approved_project`) and one
  `require_claude_login()` helper replacing the four copy-pasted skip pairs; a dedicated
  `tests/test_execution.py`.
- Delete the ghost `__pycache__/` (27 v1 `.pyc`) and `.agentctl/workspaces/`; ensure
  ignored.

**Product (P)**
- `ChangeCost`: hide until two approved spec revisions exist (real wiring is M4).
- `PreviewPanel`: send `scaffold.destination` (`ControlPlane.tsx:1047`), deploy the
  preview that was just approved rather than `latest.id` (`PreviewPanel.tsx:174`).
- Delete `web/smoke.mjs` (targets `/v2` on port 8765); real frontend tests arrive in M11.
- `V2ApiError` → `ApiError`; drop the `v2-` CSS prefix (97 selectors, mechanical).
  Stale `v2` prose in `openai_provider.py:5`, `control_plane.py:266`,
  `models/_common.py:21,552`, `planner.py:59`.

**Docs**
- `docs/architecture.md`: six gates (`:54`, `:552`), prerequisites say two routes
  (`:473`), roadmap item 5 is shipped (`:575`), invariants renumbered in order; the
  roadmap section becomes a pointer to this program.
- Delete `docs/spec.md` (describes the deleted v1). Fix `docs/testing.md:50-58`
  (`run_tests.py` does not exist). `CLAUDE.md`: `models/` is a package; add `change.py`
  and `runlog.py` to the module map. `README.md`: `rich doctor` does not check versions
  (until M11 makes it so). `nextjs.py:726-728` stale planner comment.
- Board: `tools/board_health.py`; fix header/footer disagreement, stale HEAD, stale
  OpenAI decision card; add the three releases as swimlanes.

**Exit:** suite green on 3.10–3.14, wheel installs, docs make no false claim, board
generated. **Drive:** none yet — this is the floor.

### M1 · A project you can return to, and the software you can take — P — size M

**Goal.** Close the tab, reopen tomorrow, everything is where you left it; and the one
artefact that already exists — the verified release ZIP — is one click away.

- Server: `GET /v1/projects/{id}/state` (new `control_plane.project_state`) returning
  the latest `product_spec` revision + approval, latest `architecture` revision +
  approval, runs (latest first, with status), previews. Built from store methods that
  already exist (`store.py:617`, `:675`, `:1256`).
- `GET /v1/runs/{id}/release` streams `source:release-snapshot` (attached at
  `run_engine.py:991`) with `Content-Disposition` — no base64, no 4 MiB cap
  (`api.py:46`). A **Download release** button on a succeeded run.
- Canvas: `loadProject` restores from the one state call; `SavedSession` shrinks to
  `{projectId}` + UI preferences — no more localStorage copies of server objects.
  Actor identity becomes a header chip ("Deciding as *founder*"), persisted, instead
  of a field under the Inspector. The unapplied architecture draft persists per project
  in localStorage.
- Interview draft persists **server-side** now (M2's step 1, no model yet): store
  migration 12 `interview_drafts(project_id PK, draft_revision, document_json,
  submitted_revision_id, created_at, updated_at)` with its own optimistic counter
  (`RevisionConflict` on mismatch); `GET/PUT /v1/projects/{id}/interview`. Not a
  revision kind: `save_revision` bumps `projects.current_revision` (`store.py:551-598`)
  and would race `submit_interview`'s `expected_revision`.
- `api.ts`: `projectState`, `listRevisions`, `getRevision`, `listRuns`,
  `getInterview`, `saveInterview`, `releaseUrl`.

**Exit / drive:** create → interview → approve → reload → still there; switch projects
→ each restores intact; download the ZIP of a succeeded run.

### M2 · Intent without code — P — size L

**Goal.** Drive steps 2–3: prose in, readable spec out, never a JSON or an id.
Design B below is the specification; this is the work list.

1. `interviewer.py` (new, modelled on `architect.py`): response schema, decoder,
   `answers_from_keys`, offline schema tests.
2. `propose_interview` + `ModelInterviewer` + `runtime.default_interviewer(route=…)`
   + `POST /v1/projects/{id}/interview-turns` with the `form-fallback`.
3. Canvas: `intent/steps.ts`, `ScenarioStepEditor`, `RequirementPicker`,
   `ConversationPanel`, two-column `IntentStage`; the JSON textarea and id inputs are
   deleted; `Waiting` during a turn.
4. Failure legibility: each compiled oracle step becomes a named `test.step`; the
   protected reporter emits a second `RICH_ACCEPTANCE_FAILURES` line; the engine parses
   it leniently on *failed* acceptance only; `Assurance` highlights the failing step.
5. `tests/test_interviewer_live.py`; `docs/architecture.md` §1.

**Exit / drive:** steps 2–3 with zero 🔧; a deliberately wrong oracle fails the e2e gate
and the canvas points at the step.

### M3 · Build is one button; money is visible; failure is legible — P — size M

**Goal.** Drive step 5.

- **One action.** Prepare → scaffold → execute stay three durable authority
  boundaries; the canvas runs them as one **Build** with derived defaults (destination
  `<project-slug>/run-<short-id>`, scope from the project id) and shows the three as a
  checklist inside `Waiting`. Budget is one dollar figure; the derived dimensions
  (`ControlPlane.tsx:426-430`) are shown, not hidden.
- **Cost meter.** `GET /v1/runs/{id}/usage`: reuse `recover_model_usage`
  (`providers.py:415`), which already reconstitutes totals from event history.
  Canvas: `$spent / $budget`, attempts, tokens, live.
- **Legible timeline.** `GET /v1/runs/{id}/timeline` serving `runlog.format_event`
  lines (`runlog.py:80`) — reuse, don't port. The raw JSON feed becomes a `<details>`.
- **Failure.** For a failed attempt: the gate, the redacted diagnostics (the
  `rich.command-verification/v1` artifact the retry already sees), and three next
  actions: *Build again* (prepare a new run over the same approvals; memo makes it
  cheap), *Rebuild `<node>`*, *Amend the design*.
- **Engine prerequisite (E, S):** a second build must not re-download the world.
  Today the pnpm store and Playwright browsers live inside each workspace
  (`run_engine.py:314`), so every run pays ~11 min and ~2 GiB. Move them to a shared,
  integrity-verified cache under the state dir, mounted read-only into gates after
  `--verify-store-integrity`. Fail closed on any mismatch. (Also the prerequisite for
  M14: per-task workspaces cannot each bootstrap.)

**Exit / drive:** Build with $10 → cost climbs → a forced failure reads as a sentence
with a next action → a second Build over the same revisions takes minutes.

### M4 · Amend → cost → rebuild — P (+E for the architect) — size L

**Goal.** Drive step 8. The feature that makes RICH an architect's tool *over time*.

- **Incremental redraft (E).** Change locality requires the architect to preserve
  unchanged contracts *exactly* — `change.py` compares behaviour, so a redraft that
  rewords every contract stales everything. `architect_prompt` gains
  `previous_architecture`; the response schema lets the model return only the
  components it changes; the engine carries the rest verbatim. Test: amend one
  requirement → the untouched components' contracts are byte-identical.
- **The flow (P).** Amend in the conversation → new spec revision → approve → redraft
  (incremental) → approve → `ChangeCost` computed from the previous approved pair to the
  new one (`plan_change`, `control_plane.py:177`) → **Apply and build** = `apply_change`
  (forgets stale memos) + Build. Every plan still says "every gate re-runs".

**Tests.** Offline: the flow through `control_plane` with a fake architect; live: the
existing `test_change_locality_live.py` extended to go *through the redraft* instead of
a hand-built architecture. **Drive:** step 8, cost shown before commitment.

### M7 · Persistence — E — size XL — **spike first, immediately after M0**

**Goal.** Drive step 6's *"reloads — it is still there"*. The largest engine change and
the only milestone with an unknown that could invalidate the pack, so it goes first on
the engine track. Design A below is the specification; ordered work:

1. **Lock.** `tools/refresh_nextjs_lock.py` (there is none; the module docstring is the
   only provenance): render the pack with every optional importer and the
   `@rich-template` scope, run host pnpm 10.34.5 `install --lockfile-only
   --ignore-scripts --strict-peer-dependencies`, rewrite `_nextjs_lock.py`. New edges:
   `packages/domain → ../db`; `@electric-sql/pglite` (+ `postgres`) as *direct*
   `apps/web` deps (a `serverExternalPackages` require executes from `.next/server`
   and pnpm does not hoist). **Lock-validity test:** parse the template with PyYAML,
   assert every importer specifier resolves and, for all eight `include_*`
   combinations, each rendered `package.json` matches its importer.
2. **Spike — stop here if it fails.** A live test that scaffolds, bootstraps, and under
   the *gate* policy runs `new PGlite(dir)` + DDL/DML, then `next build --webpack` and
   `next start` with a fixture-authored server-action page and one
   `fill/click/reload` scenario. Measure address space (RLIMIT_AS 16 GiB with
   `--disable-wasm-trap-handler`, already in `NODE_OPTIONS` at `run_engine.py:367-371`),
   wall time, externals resolution.
3. **Scaffold** (`nextjs.py`): protected `packages/db/src/database.ts` (factory:
   `DATABASE_URL` → postgres-js, else `RICH_DATABASE_DIR` → PGlite, else throw);
   `migrate.ts` rewritten to the shared algorithm; `migrations/0000_initial.sql`
   without `CREATE EXTENSION pgcrypto` (`:1542`; PGlite lacks it, `gen_random_uuid()`
   is core); drop `meta/_journal.json`; `next.config.mjs` `serverExternalPackages`;
   `_intent_files` (`:491-717`) marks requirements the DATA node serves as `persists`
   and emits their pages `force-dynamic`; the probe script `.rich/verify-database.mjs`
   in the infrastructure allowlist (`:77-97`); `coding.py:654-713` protects
   `database.ts` and `migrate.ts` inside `packages/db`; pack version → `1.4.0`.
4. **Gates** (`run_engine.py`): `.rich/runtime/db` writable (`:291-297`);
   `RICH_DATABASE_DIR` in the environment for unit/property/acceptance only
   (`:365-373` — a page that reads the database at *build* time should fail); a trusted
   database-prepare step before each of those three (host-side `rmtree`, sandboxed
   `pnpm -C packages/db exec tsx src/migrate.ts`, `{engine, migrations:[{file,
   sha256}]}` into the gate's evidence); after Playwright, run the probe and merge its
   `RICH_DATABASE_PROBE` line; **fail closed on a DATA node with zero rows**.
   `PinnedRunCommands` += `database_argv`, `probe_argv` (`runtime.py:103-122`).
5. **Obligations and value language.** Interface renders `name(input): O | Promise<O>`
   and suites `await` every call (`typescript_obligations.py:270-340, 481-484`);
   `ValueTypeKind` += `IDENTIFIER` (opaque, `ascii_slug`, 1..64, optional `entity`
   slot naming the referenced record), `TIMESTAMP` (RFC 3339 UTC `Z`), **`DATE`**
   (ISO calendar date) and **`DECIMAL`** (a decimal *string* with declared precision
   and scale — `Decimal` in Python, a branded string in TypeScript, compared after
   normalisation; the "money is a decimal string, never a float" invariant reaches
   generated software) across `models/types.py` (`explain`, `fitted_to`,
   `json_schema`, request schema, `from_dict`), the sampler, `_typescript_type`,
   `Behaviour.tsx`. The async interface invalidates every memo once — announced by
   the pack version bump and written down as the rule.
6. **Preview parity** (`preview.py:457-547`): `migrate` takes
   `expected_migration_digests` and fails unless the journal after apply equals both
   the snapshot's file digests and the run's recorded set; record `SELECT version()`
   majors on both sides.
7. `tests/test_persistence_live.py`: a todo app — `open_requirement → fill "Todo" →
   click "Add" → assert_visible "Buy milk" → reload → assert_visible "Buy milk"`;
   run succeeded, coverage exact, probe ≥ 1 row, evidence carries the migration digest
   set; the data node's property suite against the in-sandbox DB. Offline updates:
   `test_nextjs_pack.py` paths/importers, `test_coding.py` protected paths,
   `test_run_engine.py` gate order/env/probe fail-closed, `test_typescript_obligations`
   async rendering, `test_models`/`test_architect` request schema, `test_preview`
   parity. `docs/architecture.md` §3, §6, §7, §8.

### M13 · Vertical decomposition — E — size XL — **before identity and integrations**

**Goal.** Drive step 4's graph: *identity, tasks, notifications, web shell*. Components
become capability-shaped, so change locality holds by construction and the dependency
DAG becomes wide enough for M14 to matter. Sequenced before M8/M9 so those are built as
capabilities rather than rebuilt later.

- **Vocabulary.** A component is a *capability* with an owned path prefix on every tier
  it touches: `packages/<capability>/` (domain + data slice: schema, migrations,
  operations) and `apps/web/src/app/<capability>/**` (its pages and server actions).
  `LAYER_OWNED_PATHS` (`architect.py:88-93`) becomes a path-allocation function of the
  capability slug; ownership stays exclusive by prefix (the scaffold already rejects
  ambiguous coverage). The shared shell — `apps/web/src/app/layout.tsx`, navigation,
  globals — is owned by the root `app` node or protected.
- **Data across capabilities.** Drizzle schema per capability, migrations per
  capability with one global order (`<seq>_<capability>_<name>.sql`, applied in name
  order by the one migration algorithm); cross-capability references only through
  contracts (`identity.currentUser` consumed by `tasks`).
- **Architect and planner.** The layer enum in `architect_response_schema`
  (`architect.py:251-254`) becomes a capability list with allocation; the deterministic
  planner keeps the layered shape as the fallback. The obligation compiler already emits
  one operations module per component (`typescript_obligations.py:51-57`);
  `change.py` is component-agnostic — neither changes.
- **Scaffold.** `_intent_files` emits per-capability pages under the owning prefix;
  the health route and shell stay where they are.

**Tests.** Offline: ownership disjointness for N capabilities, migration ordering,
manifest. Live: the todo app as two capabilities (`tasks`, `identity` stub) — amend
one, the other replays. **Drive:** step 4's graph reads as capabilities.

### M8 · Identity — E — size L

**Goal.** Drive step 6's *"signs in"*, built as the `identity` capability.

- `users`/`sessions` schema in `packages/identity`, sign-in/sign-out pages under
  `apps/web/src/app/identity/`, Next.js `middleware.ts` protecting routes (protected
  input). **No new dependency:** Node's built-in `crypto.scrypt` for passwords, opaque
  session tokens in the DB, httpOnly cookies.
- Oracle unchanged: `fill` email/password, `click`, `assert_visible` — Playwright
  keeps cookies. Session secret: fixed in gates (disposable, network-off), generated by
  trusted Python per preview/production and injected as Vercel env.
- Contracts: `signIn`, `signOut`, `currentUser` with obligations (`TOTAL` over bad
  credentials; `ROUND_TRIP` create → lookup).

**Live:** `tests/test_identity_live.py` — sign up, sign in, create a task, sign out,
task hidden. **Drive:** step 6 sign-in.

### M9 · Integrations — E — size L

**Goal.** Drive step 6's Slack, built as a capability with an adapter.

- A transport *port*; in gates a **recorded-fixture transport** whose fixtures are
  compiled from the adapter contract's EXAMPLE obligations (the claim is the mock — a
  protected input); at preview/production the real `fetch` with secrets from a closed
  handle map (`SLACK_TOKEN` etc.) as Vercel env. Network stays off in every gate.
- Contract vocabulary: an operation may be marked `external` with a declared failure
  policy (the interview already asks `integration_failure_policy`).
- **A non-live, fixture-authored version** of the integration scenario so the drive is
  reproducible in CI without a real endpoint.

**Live:** `tests/test_integration_live.py` — an adapter proven against fixtures; an
opt-in preview call reaching a real endpoint. **Drive:** step 6 Slack.

### M5 · The architect edits the graph — P (+E) — size L — **after M7 and M13**

**Goal.** Drive step 4. Also lands architect *move 4* (per-component calls) as a
product feature. Sequenced after persistence and vertical decomposition because the
per-component call is asked for the capability contract shape (async operations,
identifiers, references) — asking for the old shape would mean asking twice.

- **Editable graph.** `ArchitectureGraph`: rename, edit purpose, add capability,
  remove, move requirement chips between capabilities, add/remove dependency edges.
  Every edit → `POST /v1/projects/{id}/architecture-validations` (dry-run
  `compile_decomposition` + `ContractV2.validate`, diagnostics back, records nothing)
  → shown inline. **Apply** → `revise_architecture` (`control_plane.py:486`) as today.
- **Fill in this component (E).** `architect.py` splits into `propose_decomposition`
  (capabilities, allocation, purposes, operation names — almost nothing to get wrong)
  and `propose_component_contract` (types + obligations for *one* component given its
  requirements and its dependencies' contracts; example schemas built from
  `ValueType.json_schema()` so the decoder enforces inhabitation). Route:
  `POST /v1/projects/{id}/components/{node}/contract-drafts`. The whole-architecture
  draft becomes decomposition + N component calls, parallelised.
- **Measurement.** `tests/test_architect.py` gains an *asserted* first-attempt rate
  (today it is only prose in `docs/architecture.md:105-111`).

**Exit / drive:** step 4 end to end; a component's contract read as behaviour.

### M6 · Get the software out — P + E — size M

**Goal.** Drive step 7 (the ZIP moved to M1; production is M16).

- `POST /v1/runs/{id}/repository-pushes` (E, new `repository.py`): extract the
  immutable snapshot to a temp dir, commit with run id + digest, push to a new or
  named GitHub repository. `GITHUB_TOKEN` through the same closed handle map as
  `preview.py:965-980`. Digest-bound like preview: refuses if the live tree drifted.
- Preview (P): fixed in M0; now explain what a Neon/Vercel project id is with a link,
  remember them per project, show the URL prominently. Opt-in
  `tests/test_preview_live.py` (needs tokens; self-skips).

**Exit / drive:** repo appears, preview URL opens.

### M10 · The seam and the second pack — E → K — size XL

**Goal.** The first paradigm check. The protocol is what every later pack stands on,
so it lands as soon as M7 stabilises the Next.js pack's shape.

- **`TargetPack` protocol** (`target_packs/protocol.py`): `scaffold(spec,
  architecture, options) → manifest`, `protected_paths`, `gates(node_kind) →
  commands`, `compile_obligations(contract) → suite files`, `compile_oracle(scenario)
  → acceptance files`, `release_files`, `toolchain` (identity check, bootstrap
  command). `NextJsTargetPack` conforms; `compiler.py`, `run_engine.py`, `coding.py`
  and `runtime.PinnedRunCommands` depend on the protocol, never on the class.
- **Core becomes pack-neutral:** `AcceptanceScenario.oracle` is typed per pack and
  validated by the pack; the frozen exemption in `tests/test_models_layering.py`
  (`BrowserLocator*`) shrinks to zero — the test was built to ratchet exactly this way.
  The interview's step editor and `describeStep` become pack-driven (the pack supplies
  its oracle vocabulary and sentences).
- **Python service pack** (`target_packs/python_service.py`): FastAPI service,
  capabilities as packages; gates `ruff → mypy → pytest unit → pytest properties →
  acceptance` (start uvicorn, run an **HTTP oracle**: method, path, body, expected
  status/body — data-only like the browser one). Obligations compile to a pytest suite
  with the same generator design as `typescript_obligations.py`. Toolchain: host
  Python, identity-checked; deps installed in the network-enabled bootstrap from a
  hash-pinned lock. Persistence via the same PGlite-in-gates / Postgres-at-deploy
  design through `psycopg` + a `pglite` server shim — or SQL through the pack's own
  in-process engine; decided in the pack's spike.

**Live:** `tests/test_second_pack_live.py` — the drive's spec, headless variant, through
the Python pack. **Drive:** first paradigm check.

### M11 · Ship — E — size L

**Goal.** Drive step 1.

- **Wheel** bundles the canvas: build copies `web/dist` into `richbuild/canvas/`;
  `default_web_root` (`api.py:1003`) resolves package data first, repo checkout second.
  `rich --version` and `/v1/health.version` from `importlib.metadata`. `LICENSE`
  (owner to choose), classifiers, urls. `CHANGELOG` link fixed.
- **Docker image**: ubuntu-24.04, bubblewrap, Node 22.22.3, pnpm 10.34.5 in the
  Corepack cache, the wheel, state on a volume. **Day-one risk:** Bubblewrap needs
  user namespaces, which Docker's default seccomp blocks — prove `bwrap` works inside
  the image on Docker Desktop (macOS) before anything else; ship a seccomp profile if
  needed. Inside the container bind `0.0.0.0` but publish only to the host's loopback
  (`-p 127.0.0.1:8767:8767`) and turn the Host/Origin checks **on**
  (`validate_local_request` flags at `api.py:183` are all False today).
- **`rich doctor` tells the truth:** versions (Node 22.22.3, pnpm 10.34.5 in the
  Corepack cache, user namespaces, WASM support, Python, and each installed pack's
  toolchain), each failure with its remedy command; `ok` reflects all of it.
- **CI:** wheel build + clean install smoke; eslint for `web/`; vitest component tests
  + one Playwright smoke against `rich serve` with a fake architect and interviewer;
  image build.
- **Docs:** README as a customer quickstart (install → first build → preview → amend),
  `docs/architecture.md` current, the board generated.
- Release: tag → GitHub release with wheel + `ghcr.io` image.

**Exit:** a stranger follows the README on a clean machine and reaches the canvas.

### M12 · The customer drive — both — size M — **release 2.0**

Run the drive once on the dev host first (so an image defect is not mistaken for a
product defect), then in full on a fresh container by following the README only. Time
it. Re-audit the survey's 17 steps: every 🔧 must be gone. Then the first paradigm
check. Tag `2.0.0`.

---

## Release 2.1 — it proves more and ships to production

### M14 · Parallel coding across worktrees — E — size XL

**Goal.** Drive step 5's *"four components build at once"*. `max_workers` is
hard-pinned to 1 (`run_engine.py:504-508`) with the stated condition: "task-isolated
worktrees plus a trusted merge/reverification phase". Worth doing only now: the layered
DAG was a chain of width two; capability components (M13) make it wide.

- **Per-task workspace, not a shared directory.** RICH's source of truth is the CAS,
  not the filesystem. A task's workspace is *materialised*: scaffold manifest + the
  committed generated artifacts of its dependencies (the only source it may see —
  the firewall shows it their *contracts*; the gate needs their compiled interface,
  which is why dependencies are materialised, not siblings) + its own owned paths
  writable. Non-root gates run there. Because ownership is exclusive by prefix, the
  **merge is a union, never textual**: the root task's workspace is scaffold + every
  committed artifact, and the root gates are the trusted reverification — exactly what
  runs today, over a set that was assembled deterministically.
- **Scheduling.** The scheduler already orders by dependency; it now dispatches every
  ready task to a bounded thread pool (`max_workers` from config). One run lease, one
  fenced owner, one in-process `BudgetLedger` under a lock; per-task CAS transactions
  are already independent. Cancellation and lease loss terminate every task's process
  group through the existing path.
- **Bootstrap once.** The shared cache from M3-E is mounted read-only into every
  per-task workspace; a per-task `pnpm install --offline` from the verified store
  materialises `node_modules` in seconds.
- **Evidence.** Every task's evidence already carries its attempt and task id; the
  run's acceptance coverage is still the root's and only the root's.

**Tests.** Offline: two sibling tasks with a fake worker run concurrently and cannot
see each other's owned paths; a sibling failure blocks only its dependents. Live: the
drive's app with four capabilities — wall-clock less than the sequential run, same
evidence set. **Drive:** step 5.

### M15 · Richer contract semantics — E — size XL

**Goal.** Drive steps 4 and 6's *state machine*, *sequence* and *refusal* claims. The
obligation vocabulary (`ObligationRelation`, `models/_common.py:329-339`) is pure;
persistence is currently proven only by e2e + probe. These relations close that gap and
turn "the architect's contract" into something a wrong implementation reliably fails.

- **`SEQUENCE`** — a list of calls sharing a context with an expected final result
  (*create x; list contains x*). The "insert then read" claim the persistence design
  could not state. Compiles to an async property test against the in-sandbox database
  with a fresh schema per case.
- **State machines** — a contract may declare an entity's `states`, a `stateOf`
  operation, and `transitions: (op, from, to)`. Obligations: `TRANSITION(op, from, to)`
  and `REFUSES(op, from)`. Compiled by driving the entity to `from` along a declared
  path, calling `op`, asserting `stateOf`. Requires M7's identifier/reference kinds.
- **`ATOMIC(op)`** — for every prefix-failure of the repository calls `op` makes,
  observable state equals the initial state. Feasible because the data slice is reached
  through a port: the suite wraps the repository in a proxy that throws on the *n*-th
  write. Depends on a scaffolded `withTransaction` (protected) in `database.ts`.
- **`CONCURRENT(op)`** — two interleaved calls leave a state equal to one serial
  order. **Caveat stated, not hidden:** PGlite is single-connection, so this needs a
  multi-connection database. Add an optional **real-Postgres gate** using a pinned
  Postgres in the trusted tool bundle (operator-installed like Node; identity-checked;
  Unix socket inside the sandbox; never downloaded) that runs *only* the concurrency
  suites. The sandbox surface is paid once, for the property that needs it.
- Canvas: `Behaviour.tsx` renders each as a sentence; the interview and the architect's
  per-component call offer them only where the types make them legal (the same
  "offer only legal relations" discipline as today).

**Tests.** Offline: each relation compiles to a suite that a hand-written wrong
implementation fails and a right one passes (fixtures in
`tests/test_typescript_obligations.py`'s style). Live: the drive's task state machine
proven; an atomicity violation caught. **Drive:** steps 4, 6.

### M16 · Production promotion — E + P — size L

**Goal.** Drive steps 7–8's *promote*, *health*, *rollback*.

- **A new gate.** `ApprovalGate.PROMOTION` (`models/_common.py:240-256`) binding the
  same release digest a preview was verified and observed healthy on, plus the
  production targets (Neon project + main branch, Vercel production alias).
- **`promotion.py`** (modelled on `preview.py`'s orchestrator, `:617-699`), durable
  `promotions` table, steps each recorded as events: (1) rollback point = a Neon branch
  from `main` at promotion time; (2) migrations applied to `main` by the one algorithm
  with the digest journal — the journal must already contain every prior migration, so
  a promotion cannot skip one; (3) Vercel *production* deployment of the identical
  immutable snapshot; (4) health: the scaffold's `/api/health` route plus the
  read-only subset of the approved acceptance oracle (`assert_*` steps only, tagged
  `safe_in_production` by the pack) run against the live URL; (5) **automatic
  rollback** on any failure: re-alias the previous deployment, restore `main` from the
  rollback branch, record it. Secrets through the closed handle map.
- **Backward-compatible migrations** become a contract concern: a migration that drops
  or renames a column consumed by the *previous* release fails promotion planning
  (checked by trusted Python from the two snapshots' schemas), so a rollback never
  meets a schema it cannot read.
- Canvas: a **Promote** panel with the rollback point, health result, and a one-click
  rollback; CLI `promote`/`rollback`.

**Tests.** Offline: the orchestrator against fake Neon/Vercel transports — journal
continuity, rollback on health failure, rollback on migration failure, secrets never in
messages. Live: opt-in, needs tokens. **Drive:** steps 7–8. Tag `2.1.0`.

---

## Release 2.2 — any software

Every pack below is a `TargetPack` (M10) living in `target_packs/<name>.py` plus its
lock/toolchain, its obligation compiler, its oracle vocabulary, and one live test that
builds a small real program. Track K: one worktree per pack; they are independent and
run in parallel. The core changes only through the protocol. Ordered by mechanism
reuse and by how much of the paradigm each one proves.

### M17 · TypeScript library pack — K — size M
The cheapest pack and the one that separates "web" from "TypeScript": no Next.js, no
browser. Reuses the Node toolchain, the lock discipline and `typescript_obligations.py`
verbatim. Gates: `eslint → tsc → vitest unit → properties → build (tsup) → acceptance`
where the oracle is a **CLI/API invocation oracle** (call `bin` or import, expect
stdout/return). Release: a publishable `dist/` + `package.json` — protected — with the
digest in the version metadata.

### M18 · Lean pack — the `PROOF` tier made real — K — size XL
The deepest instance of the thesis. `ObligationTier.PROOF` exists and the TypeScript
compiler refuses it (`typescript_obligations.py:360-366`) because a sampled check
cannot discharge a proof. A **Lean 4 target pack** builds software *in* Lean:
capabilities as Lean modules, the contract's operations as definitions, and every
`PROOF`-tier obligation compiled to a `theorem` the model must prove — checked by the
kernel, which is the purest form of "model output is never evidence" this project can
have. `SAMPLE`-tier obligations still compile to `#eval`-driven property checks so a
contract can mix the two. Gates: `lake build` (proofs + code), `lake test`, acceptance
by CLI oracle. Toolchain: pinned `elan`/`lake` in the trusted bundle, operator-
installed, identity-checked, never downloaded. A later assurance option lets the
Next.js pack carry a Lean model of a contract alongside the TypeScript implementation
(proof of the contract's coherence, sampled conformance of the implementation — stated
exactly that way).

### M19 · Rust pack — K — size L
`cargo` + `clippy -D warnings` + `cargo test` + obligations compiled to `proptest`
strategies from the same generator design + acceptance by CLI/HTTP oracle. Toolchain:
pinned `rustup` toolchain in the trusted bundle. Release: the built binary plus its
`Cargo.lock`, digest-bound.

### M20 · Data / ML pipeline pack — K — size L
Python; capabilities as pipeline stages with **data contracts** (schemas as value
types over rows); obligations compile to checks over fixture datasets (`PRESERVES` row
counts, `ESTABLISHES` output schema, `SEQUENCE` stage chains); acceptance = run the
pipeline on the fixture and assert the output contract; release = the pipeline package
plus a pinned environment. Gates network-off; real sources only at deploy through the
handle map.

### M21 · Infrastructure pack — K — size L
Terraform or Pulumi; capabilities as stacks; obligations compile to **policy checks**
(OPA/conftest — "every bucket is private", "every database has backups"); acceptance =
`plan` against a fixture provider with the expected resource graph; deploy = the
promotion path (M16) with the plan digest bound to the approval and `apply` as the
promoted step, rollback = the previous state file.

### M22 · Mobile pack — K — size XL
Expo/React Native; the acceptance oracle runs on **Expo web under Playwright** inside
the sandbox (the same vocabulary as the Next.js pack). **Caveat stated:** native
acceptance needs an emulator that cannot run inside Bubblewrap; it is a preview-time
device-farm step — external, approval-gated and digest-bound like a deploy — not a
gate. Release: an EAS build from the immutable snapshot.

**Release 2.2 exit:** the paradigm checks in the drive — one small real program per
pack, each from an approved spec through its own gates to a digest-bound artifact.

---

## Design decisions

### A. Persistence in a network-off sandbox

- **Layering: `domain → db`, never `web → db`.** Mirrors the edge that already exists
  (`LAYER_DEPENDENCIES`, `architect.py:106-110`; `planner.py:279-286`). The repository
  port *is* the data node's pinned `Operations` interface
  (`packages/db/src/operations-contract.ts`); `domain` implements use cases against
  that contract; `web` calls `domain` through **Server Actions** (`<form action>`
  needs no client JS, so `fill/click/assert_visible` stay robust). Change locality
  falls out of `change.py:153-173, 257-261`: a schema or repository change inside
  `packages/db` is invisible to `domain` and `web`; only a contract change propagates.
  After M13 the same holds per capability slice.
- **Database: PGlite in gates, Postgres on Neon at preview and production.**
  Postgres-in-WASM, in-process, socket-free, filesystem-backed — needs neither network
  nor a daemon under `--unshare-net`; the dialect *is* Postgres, so the migration that
  passed the gate is the text psycopg applies to Neon. Rejected: a Postgres binary in
  the trusted bundle (writable `PGDATA`, `initdb`, a socket, locale files, and a
  download RICH is forbidden to make — every one a new sandbox surface; revisited in
  M15 for the one property that needs multiple connections); SQLite (changes the
  dialect, so the verified migration is not the applied one).
- **One migration algorithm, two trusted implementations.** `preview.py:519-540`
  already applies `packages/db/migrations/*.sql` in name order, splits on
  `--> statement-breakpoint`, journals `(filename, sha256)`. The protected gate-side
  `migrate.ts` does exactly that; drizzle's `_journal.json` is dropped so there is no
  second semantics for the model to keep consistent. Preview and promotion assert the
  journal they wrote equals the digest set the run's acceptance evidence recorded.
- **`reload` proves the record outlived the request, not that it hit the database.**
  Hence the trusted **persistence probe** after Playwright, in the same sandbox: opens
  the PGlite directory, prints `RICH_DATABASE_PROBE {tables, migrations, version}`;
  the engine attaches it to acceptance evidence and fails closed on a DATA node with
  zero rows. "Model output is never evidence", extended to data. M15's `SEQUENCE`
  relation makes the same claim provable at the contract level.
- **Value language:** `identifier` (opaque, optional `entity` reference slot),
  `timestamp`, `date`, `decimal`. Nothing else until a scenario needs it.
- **Operations are async-tolerant uniformly** (`O | Promise<O>`); PGlite is async and
  `domain` awaits `data`, so kind-scoped async would not stay scoped.

### B. A model-driven interview that authors the oracle

- **One bounded call per turn** in `interviewer.py`, modelled on `architect.py`:
  `GenerationRole.INTERVIEWER` (exists, `providers.py:20`); reserve through the
  gateway; two attempts per turn with `InterviewIncomplete`'s text as the repair;
  **never raise** — outcome is `complete | questions | partial` with rejections
  carried; with no model route the deterministic `next_questions`
  (`interview.py:136-159`) answers, tagged `source: "form-fallback"` exactly like
  `planner-fallback`.
- **Invalid steps are unrepresentable.** `oracle` items are an `anyOf` of twelve
  branches, one per `AcceptanceAction`, each `additionalProperties: false` with
  `required` derived from `_LOCATOR_ONLY_ACTIONS` / `_LOCATOR_VALUE_ACTIONS` /
  `_VALUE_ONLY_ACTIONS` / `_NO_ARGUMENT_ACTIONS` (`models/spec.py:133-157`); the
  `role` locator's `value` is the `_ARIA_ROLES` enum (`models/_common.py:52-137`);
  `navigate`/`assert_url` carry the local-path pattern (`spec.py:206-216`).
  `AdaptiveInterview.compile` (`interview.py:161`) is unchanged — only what feeds it.
  After M10 the vocabulary comes from the pack.
- **Keys, not ids.** Requirements and scenarios carry a model-chosen `key`
  (`^[a-z][a-z0-9-]{0,40}$`); scenarios reference `requirement_keys`; on later turns
  that slot is an enum of the draft's keys (the `_requirement_id_schema` move,
  `architect.py:294-297`). Ids (`req.<key>`, `scenario.<key>`) are derived once by
  `answers_from_keys` and never shown.
- **The prompt states what the generated app will have** (`/` and
  `/capabilities/<slug>`, `nextjs.py:456-459`) and that the labels and button names
  it writes become demands the implementer is shown (`coding.py:1486-1490`).
- **Canvas:** conversation left, structured draft right; `steps.ts:describeStep`
  renders each step as a sentence with a Python twin `AcceptanceStep.describe()` in
  `models/spec.py` feeding the `test.step` titles, so the canvas and the Playwright log
  say the same words. Dropdowns map 1:1 to the enums; JSON is generated, never typed.
- **Failure legibility:** the protected reporter (`nextjs.py:1142-1172`) gains
  `onStepEnd` and a second `RICH_ACCEPTANCE_FAILURES` line; the strict coverage parser
  (`run_engine.py:137-194`) is untouched; success semantics unchanged.

---

## Invariants touched, and how each is preserved

| Invariant | Milestones | Preserved by |
|---|---|---|
| No network in gates | M7–M9, M15, M17–M22 | in-process PGlite; recorded-fixture transports; fixture datasets/providers; secrets only at preview/production; the M15 Postgres gate is a Unix socket inside the sandbox |
| Model output is never evidence | all | new gates are command results; PROPERTY added to the forbidden set (M0); the persistence probe is a trusted command; Lean proofs are checked by the kernel |
| Protected generation inputs | M7–M9, M13 | `database.ts`, `migrate.ts`, `withTransaction`, `middleware.ts`, fixtures, locks — protected; migrations are owned source verified in-gate and digest-bound to release, preview and promotion |
| Firewall / change locality | M7, M13, M4, M14 | data reached only through contracts; capability ownership exclusive by prefix; per-task workspaces materialise dependencies' artifacts only; incremental redraft preserves untouched contracts byte-for-byte |
| One fenced owner mutates a run | M14 | one run lease; the pool is threads under that owner; every write still fenced in-transaction |
| One canonical encoding | M0 | providers use `canonical.py` |
| Generation memoized, verification never | M7, M14 | the async interface changes memo keys once (stated as the pack-version rule); parallelism never reuses a verdict |
| Toolchain never downloaded | M10, M11, M15, M17–M22 | every pack's toolchain is operator-installed and identity-checked; the image ships the pins |
| Loopback / host / origin | M11 | checks enforced, not optional, when bound beyond loopback |
| Money is decimal | M3, M7 | usage route sums `Decimal` strings; the `decimal` value kind carries it into generated software |
| Approvals bind exact revisions | M1, M2, M16 | drafts are not revisions; promotion is its own gate over the exact verified digest |

---

## Verification, end to end

Per milestone: `ruff check .` · `python -m pytest` (3.10–3.14 in CI) · `npm --prefix
web run typecheck && run build` · the milestone's live test · its drive steps, recorded
on the board card with the commit.

Live tests added (all `live`, self-skipping): `test_interviewer_live`,
`test_persistence_live`, `test_vertical_live`, `test_identity_live`,
`test_integration_live`, `test_second_pack_live`, `test_preview_live`,
`test_parallel_live`, `test_semantics_live`, `test_promotion_live`, and one
`test_<pack>_live` per pack. **Always pass `--basetemp` on real disk** — today's outage
was a 3 GiB tmpfs quota filled by a live workspace.

Release gates: **2.0** fresh image → README verbatim → the drive → first paradigm
check. **2.1** the drive's parallel, semantics and promotion steps live. **2.2** one
real program per pack.

**Budget for proof.** Each live build is minutes and dollars (or `claude` quota).
Expect ~120–180 live builds across the program; on the `api` route roughly $400–800.

---

## Sizing and order

| Track P (this session) | Track E (worktrees) | Track K (packs) |
|---|---|---|
| M0-P · M0-docs | M0-E | — |
| M1 return + ZIP (M) | **M7 spike** (S) → M7 persistence (XL) | |
| M2 interview (L) | M3-E shared cache (S) | |
| M3 build/cost/failure (M) | M13 vertical (XL) | |
| M4 amend/cost/rebuild (L) | M8 identity (L) → M9 integrations (L) | |
| M6-P preview polish (S) | M10 seam + Python pack (XL) | |
| M5 graph editing (L) — after M13 | M6-E repository push (S) → M11 ship (L) | |
| **M12 drive → 2.0** | | |
| M16-P promote panel (S) | M14 parallel (XL) → M15 semantics (XL) → M16 promotion (L) | M17 TS library (M) |
| **→ 2.1** | | M18 Lean (XL) · M19 Rust (L) |
| | | M20 data (L) · M21 infra (L) · M22 mobile (XL) |
| | | **→ 2.2** |

≈ 55 agent-days to 2.0, ≈ 25 more to 2.1, ≈ 45 more to 2.2 — about 125 sequential;
roughly 70–80 wall-clock across the tracks, packs being fully parallel. Dependencies:
M13 before M8/M9 (built as capabilities); M5 after M7 and M13 (contract shape); M14
after M13 (a wide DAG) and M3-E (shared cache); M15 after M7 (references, async) and
M13 (entities per capability); M16 after M7, M8 and M6; every K milestone after M10.

## The one thing outside this program

A hosted, multi-tenant service. Decision 1 above: customers run RICH locally or in the
image. Everything else the roadmap ever named is a milestone here.
