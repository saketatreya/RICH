# RICH architecture and operating contract

## Mission

RICH is a kernel for generalizing software development as a controlled
transformation:

> approved intent → owned implementation work → independently verified release

The central design choice is that code generation is neither the source of truth nor
the judge of success. Product intent, architecture, authority, budgets, source
ownership, execution, evidence, and release identity are separate typed objects with
durable links between them.

This creates a reusable compiler boundary:

- a front end turns domain intent into requirements and acceptance scenarios;
- an architecture planner turns those requirements into contracts, ownership, and a
  dependency graph;
- a target pack defines one language/toolchain/release environment;
- bounded agents fill approved source-owned regions;
- a trusted verifier produces evidence;
- a release adapter freezes and deploys the exact verified source.

The implemented target pack is currently a Next.js 16 monorepo. Generalization comes
from adding target and resource packs without weakening the invariants below.

## End-to-end state machine

```text
draft project
  │ structured interview
  ▼
product-spec revision ── product_spec approval ──┐
                                                │
                                                ▼
                                      architecture revision
                                                │
                                  architecture approval
                                                │
                                                ▼
                                    compiled durable run
                                  + complete run budget
                                                │
                                      target-pack scaffold
                                                │
                                   fenced execution lease
                                                │
                      ┌─────────────────────────┴─────────────────────────┐
                      │ dependency-ordered bounded coding tasks          │
                      │ source-only model output + durable reservations  │
                      └─────────────────────────┬─────────────────────────┘
                                                │
              lint → types → unit → contract obligations → production build → browser
                                                │
                                  content-addressed evidence
                                                │
                                  immutable release-source ZIP
                                                │
                                      preview approval
                                                │
                               Neon branch + Vercel deployment
                                                │
                                      expiry / teardown
```

A transition fails closed when any identity, approval, ownership, budget, lease,
sandbox, evidence, or digest check is missing or inconsistent.

## Core invariants

### 1. Intent is immutable and approved

A submitted interview compiles to a `ProjectSpecV2` revision. Requirements have stable
IDs, priorities, and statements. Each acceptance scenario combines human-readable
Given/When/Then intent with a mandatory, data-only browser oracle. The oracle supports
bounded navigation, locator-based interaction, keyboard actions, reloads, and observable
URL/visibility/focus/text/value assertions. It cannot embed JavaScript or access an
external URL. Architecture cannot be proposed until the exact spec revision is approved.

The answers that compile into that revision are authored in a conversation
(`interviewer.py`), not typed as a form. One bounded model call per turn returns either
questions or a complete candidate, in a response schema shaped so that an invalid oracle
step is unrepresentable: one branch per acceptance action carrying exactly that action's
fields, locator kinds and ARIA roles as enums. The model names things by short keys; the
stable ids are derived from them (`answers_from_keys`) and a person never sees one. The
validator is unchanged — `AdaptiveInterview.compile` still decides — and a rejected
candidate is retried once with the validator's own message, then returned as `partial`
with the rejections attached rather than raised, so the person finishes it in the editor.
With no model route (`rich serve --route none`) the deterministic questions answer, tagged
`form-fallback`. The conversation and the structured answers live on the server as one
mutable draft per project with its own optimistic revision (`interview_drafts`) — not a
revision kind, because revisions are append-only and bump the project's counter, so
autosaving every edit as one would race the submission's `expected_revision`.

Every compiled oracle step is a named Playwright step titled with the sentence the person
approved — "3 · Expect to see the text ‘Buy milk’" — rendered by `describe_step` in the
pack and by `describeStep` in the canvas from the same data, held to each other by one
fixture. On failure the protected reporter emits a second, explanatory line
(`RICH_ACCEPTANCE_FAILURES`) that the engine reads leniently and only for a failed
acceptance command; the coverage line it trusts is untouched.

Revisions are append-only. Approval records identify the gate, project, exact revision
claims, actor, decision, reason, and time. Approving one revision never authorizes a
later revision.

### 2. Architecture owns behavior and files

Every non-resource architecture node has a typed contract, requirement ownership, and
source ownership. Edges express call, data, event, or resource dependencies. The
compiler validates the entire graph before emitting tasks and orders providers before
consumers.

Resource nodes describe externally provisioned capabilities; they do not become coding
tasks and their paths cannot authorize generated source. The Next.js target pack rejects
missing ownership, traversal, globs, ambiguous coverage, and source outside an approved
non-resource owner.

### 3. The architect is asked only what it can get right

Architecture may be authored by a model (`architect.py`) or by the deterministic layer
planner (`planner.py`). The model's proposal is the *input* to the compiler, never a
decision: `compile_decomposition` and `ContractV2.validate` still check the whole graph
before anything is emitted. What changed is the division of labour between what the model
is asked for and what the system derives.

Measured over six first attempts on one fixed spec, asking the model for everything
assembled 0/6. Five of six rejections were a value failing to inhabit a type the same
model had invented a few thousand tokens earlier — not one was a design mistake. The
architect had become the only component in RICH that judged correctness at admission time
rather than by execution, which is the inverse of the thesis everything else follows.

Three rules follow, and together they moved that number to 6/6:

- **The architect always has a valid answer.** `propose_architecture` never raises. When
  no proposal assembles, it returns the deterministic planner's layer shape tagged
  `source: "planner-fallback"`, carrying the rejections as risks. The human sees a
  baseline design and what went wrong, not an error. This is v1's `{"is_leaf": true}`:
  a degraded answer that is always structurally valid.
- **Evidence beats declaration.** Character sets and length bounds are *derived* from the
  declared example values (`ValueType.fitted_to`), not asked for. A model's example is
  stronger evidence of its intent than its guess at a bound, and derivation makes
  "example violates its own bound" unrepresentable rather than rejectable.
- **A claim that cannot be expressed is dropped, not fatal.** Obligations that fail to
  typecheck against the fitted operations are removed and recorded in the architecture
  metadata as `dropped_obligations`; EXAMPLE obligations are never dropped, so the
  anti-vacuity rule still binds. Losing one claim is a smaller loss than losing the design.

None of this weakens what is checked. Anti-vacuity, endomorphism, predicate typing, and
example inhabitation all still hold on the assembled architecture, and every surviving
obligation must still compile to a runnable check.

### 4. The scaffold freezes the verifier

The target pack emits:

- exact package manifests and a lockfile snapshot;
- TypeScript, ESLint, Vitest, Playwright, Next.js, and workspace configuration;
- requirement-derived unit tests and browser tests compiled from approved oracle steps;
- a protected reporter that emits only actually passed scenario IDs, bound to the exact
  run, task, attempt, and nonce;
- source ownership metadata;
- an exact file manifest with SHA-256 identities.

The manifest itself is stored in the content-addressed artifact store. Before execution,
the run engine reconstructs the expected workspace identity and rejects missing,
unrecorded, altered, oversized, or symbolic-link files.

Model output is restricted to approved owned source paths. Package manifests, lockfiles,
tests, type declarations, compiler configuration, framework configuration, CI, and RICH
metadata are protected generation inputs.

**Persistence is scaffolded, not improvised.** When the architecture has a data
component the pack renders `packages/db`, and two files inside it are protected even
though the data node owns the directory: `src/database.ts`, the only place an engine is
chosen (`DATABASE_URL` → Postgres over the wire; else `RICH_DATABASE_DIR` → PGlite,
Postgres compiled to WebAssembly, in-process and socket-free; else throw — there is no
default, so a page that reads the database while `next build` prerenders it fails, as it
should), and `src/migrate.ts`, the gate-side half of the one migration algorithm
(`packages/db/migrations/NNNN_name.sql` in name order, split on `--> statement-breakpoint`,
one transaction, a journal of `(filename, sha256)`; an applied file whose content changed
is refused). Each driver is imported only once selected, so a preview server never
evaluates the WebAssembly engine. Drizzle's `meta/_journal.json` is not rendered: the SQL
files and their digests are the only migration state, so there is no second bookkeeping
for a worker to keep consistent. The factory is handed to the data task the way the pinned
operations interface is — as context, never as one of its current files — because a
worker told to persist without being shown the one door to the database would invent a
second one. Requirements a data component serves are marked `persists` in the compiled
intent and their pages are rendered `dynamic = "force-dynamic"`. Beside the manifest
verifier sits `.rich/verify-database.mjs`, the read-only persistence probe §8 describes.
The persistence pack is target-pack version 1.4.0; every remembered generation was
invalidated once by that bump, deliberately, because what a worker is shown and held to
changed.

### 5. Authority and budget are explicit

Preparing a run binds:

- project/spec/architecture revision IDs;
- the architecture approval;
- target-pack identity;
- the exact compiled node/task set; and
- a complete budget.

The budget must contain exactly:

```json
{
  "max_model_attempts": 12,
  "max_input_tokens": 384000,
  "max_output_tokens": 96000,
  "max_cost_usd": "10.00",
  "max_execution_seconds": 1800
}
```

Money is a decimal string, never a binary float. The default coding attempt reserves
32,000 input tokens, 8,000 output tokens, $0.208, and 120 seconds. Prompt content is
limited to 24,000 bytes; the Anthropic adapter then counts the complete canonical request
envelope, response schema, and framing allowance before any HTTP request. Before a
provider call, the gateway durably records and reserves the maximum attempt usage. It
then settles actual usage. A provider-reported overage is recorded exactly and blocks
further work rather than being clipped to the reservation. On restart, a started attempt
without a settlement is conservatively charged at its reservation, so a crash cannot
create free retries.

The trusted default accepts only `anthropic/claude-sonnet-5`. Pricing is explicit in
code and lists all four input classifications separately — base input, 5-minute cache
write, 1-hour cache write, and cache read — so an attempt is charged the exact mix the
provider reports; when the cache-write breakdown is absent or does not reconcile, every
cache-write token is charged at the costlier 1-hour rate. Reservations above the model's
context window are refused, credentials are resolved only at the network boundary, and
there is no provider/model fallback. The model choice follows the current official
[Claude model pricing](https://platform.claude.com/docs/en/about-claude/pricing).

`max_tokens` on the Messages API bounds thinking and response text together, so an output
reservation that is tight for the response alone truncates. That surfaces as a
non-retryable "response was incomplete (max_tokens)" failure, never as a silently short
file. `output_config.effort` is always sent explicitly so the request envelope is fully
determined by RICH rather than by a server-side default.

`openai_provider.py` remains in the tree but is wired to nothing. It is the second
implementation that keeps the `ModelProvider` seam honest, not a fallback path.

Two routes reach that one model policy, selected explicitly and never substituted
for one another. The `api` route is a bounded HTTPS request and needs an
`ANTHROPIC_API_KEY`. The `claude-code` route runs `claude -p` against an existing
Claude Code login, so a subscription can pay for a run; a run record names which
answered, because the trust properties differ. That route pays three stated
prices. The worker is stripped to a text generator with `--tools ""` in an empty
working directory under a throwaway `HOME` containing only a symlink to the
credential — measured, not assumed: with the real `HOME` the worker reports back
the operator's own `CLAUDE.md` memory, which is exactly the unapproved context the
information firewall exists to exclude. A residue of two items, the account email
and the current date, remains and is recorded rather than denied. And the CLI
exposes no maximum-output control, so on that route the output reservation is
advisory: an overage is detected and charged after the fact rather than prevented.
The harness's own auxiliary small-model calls are charged too, and the pinned model
is verified to have out-generated them.

### 6. One fenced owner mutates a run

Execution claims an expiring SQLite lease containing an opaque fencing token. The owner
heartbeats during work. A stale owner cannot renew or release a successor's lease, and
coding checks the token before the provider call, after the call, and before/after a
filesystem mutation.

The same token fences every authoritative scheduler transition, event, evidence link,
and run-artifact attachment in the exact SQLite transaction that performs the write.
There is no check-then-write authority window. Losing the lease cancels active work and
terminates the complete Bubblewrap process group with bounded `TERM`/`KILL` escalation;
the executor reaps it before returning. Bootstrap and every verification command receive
the cooperative cancellation source and the scheduler's monotonic attempt deadline, so a
timed-out or stale owner cannot leave a package manager, build worker, web server, browser,
or grandchild writing into a successor's workspace.

Source writes use a CAS-backed write-ahead transaction. Original bytes and the proposed
generated-source artifact are durably prepared under the active fence before mutation;
commit attaches the exact generated artifact atomically under that fence. A successor
rolls back any still-prepared transaction before validating the protected tree, and only
when every path still matches either the original or intended digest. Cancellation or
lease loss therefore cannot strand an unrecorded workspace or let a delayed provider
write after another executor takes ownership.

The current live-workspace engine uses one coding worker at a time. Parallel coding will
require task-isolated worktrees plus a trusted merge/reverification phase; it is not
implemented by racing agents against one directory.

### 7. Sandboxing has no permissive fallback

Linux Bubblewrap is mandatory for the production runtime. Generated commands see:

- a read-only workspace;
- read-only, identity-checked Node 22.23.2 and pnpm 10.34.5 bundles;
- namespace-only aliases for the exact pnpm executable;
- explicit writable runtime/build/report paths;
- no network during lint, typecheck, unit, build, or browser verification;
- bounded wall/CPU time, processes, file size, logs, V8 heaps, and virtual address
  space;
- a database, exactly where the software runs. `BubblewrapCommandRunner` decides the
  environment and the writable set *per command kind* (`environment_for`,
  `writable_paths_for`): the unit, property and acceptance gates, and the two trusted
  database steps around them, get `.rich/runtime/db` writable and `RICH_DATABASE_DIR`
  set; lint and typecheck run no code and get neither; **build gets neither on
  purpose** — the deployed build has no database at build time either, so a page that
  reads one while `next build` prerenders it fails here, not in production. A variable
  that reaches one gate and not another is a decision that table records.

Dependency and Chromium installation is a distinct network-enabled bootstrap. It uses
the frozen lockfile, strict peers, store-integrity verification, ignored lifecycle
scripts, bounded fetch concurrency/retries, and one pnpm import worker.

The pnpm store and the Playwright browsers live in one shared cache per state directory
(`<state>/../cache`, beside the workspaces), mounted below `/opt/rich-cache`: writable only
during that trusted bootstrap — pnpm's and Playwright's own installers, lifecycle scripts
disabled, no generated code running, under a lock so two bootstraps never write it at
once — and read-only for every gate, so a run's own source can never alter the store a
later run installs from or the browser a later run is judged by. A second build installs
from what the first one downloaded instead of downloading the world again.

All sandbox processes run in dedicated process groups. Caller cancellation, lease loss,
and timeout share the same bounded termination-and-reaping path.

`RLIMIT_AS` measures reserved virtual address space, not resident memory. ARM64 Node
workers reserve an 8 GiB pointer-compression cage *per V8 isolate* before shared
libraries and build tracing — and a process running module-customization hooks
(`--import tsx`, which the trusted migrator uses) runs them on a worker thread, a
second isolate with a second cage — while Chromium reserves a much larger
PartitionAlloc cage. RICH therefore combines:

- 1.5 GiB Node V8 heap limits;
- a finite 24 GiB Node address-space ceiling. Measured on the persistence spike: the
  migrator's node peaked at 15.65 GiB (two cages beside PGlite's WebAssembly heap) and
  a plain `next build` at 16.0 GiB against the previous 16 GiB ceiling, surviving on the
  allocator's retry, which is not headroom; the resident peak of the same processes was
  under 1 GiB. The ceiling still stops a runaway mapping;
- one Next.js build CPU and no webpack build worker;
- output-file tracing rooted at the web application, excluding the mutable
  workspace package cache while approved workspace packages are transpiled;
- one sequential browser worker with a 512 MiB browser JS heap;
- bounded process counts; and
- separate finite virtual-address ceilings for Node gates and Chromium acceptance.

The production build uses supported `next build --webpack`, and browser acceptance starts
the resulting production server. This tests the same artifact produced by the build gate.

### 8. Model output is not evidence

Each task is checked by the trusted command runner. Non-root tasks require static and unit
evidence. The root release task requires all of:

1. ESLint with zero warnings;
2. TypeScript with no emit and no incremental cache write;
3. Vitest requirement tests;
4. the proof obligations every approved contract declares, when any were
   scaffolded (see below);
5. a production Next.js build; and
6. Playwright scenarios executing every approved data-only browser oracle.

Command, exit status, timeout, bounded stdout/stderr, duration, task attempt, and
requirement/scenario coverage become content-addressed evidence artifacts. An exit code
of zero is insufficient: the protected reporter must emit exactly one sorted, duplicate-
free set of actually passed scenario IDs, with the expected run/task/attempt nonce.
Missing, stale, forged, skipped, duplicated, unknown, or partial coverage fails closed.

The run cannot publish success merely because a handler returns success. The scheduler
checks required evidence kinds, blocking status, artifact roles, and exact acceptance
coverage before committing task and run status.

**Persisted state is observed, not inferred.** When the approved architecture has a data
component, every gate that runs the software — unit, property, acceptance — is preceded
by a trusted *prepare* step: the engine removes `.rich/runtime/db` on the host, computes
the migration set from `packages/db/migrations` itself, and runs the protected migrator
in the sandbox (`pnpm -C packages/db exec tsx src/migrate.ts`). The migrator's
`RICH_DATABASE_MIGRATIONS` line is a command result, not a claim: its `(file, sha256)`
set must equal the host's exactly, or the gate is not run and fails as "database
preparation failed". The set the gate ran against — `{engine, migrations}` — is recorded
on that gate's evidence, and preview and promotion assert the journal they write equals
it (§12). After the browser has run every scenario, the read-only *probe*
(`.rich/verify-database.mjs`) reports the journal and a row count per table in the same
sandbox; its `RICH_DATABASE_PROBE` line is merged into the acceptance evidence, its
journal must equal the prepare step's, and **a data component whose tables are all empty
fails acceptance closed** — `reload` proves a record outlived the request; only the probe
proves it reached the database. Neither step is an evidence kind of its own, so there is
nothing a worker could claim in its place.

### 9. A change costs what it changes

Software is not built once. Given two approved revisions, `change.py` computes the
smallest set of components that must be regenerated; everything else replays from memo.

What matters is not that some nodes are stale but *why the blast radius stops where it
does*, and the answer is the information firewall. A worker is shown its dependencies'
contracts and never their source, so:

- a node whose **implementation** changed cannot affect its consumers, because none of
  them was ever shown that implementation; and
- a node whose **contract** changed invalidates every consumer transitively, because the
  contract is exactly what they were shown.

That is a compositional guarantee falling out of a discipline already enforced, not a
heuristic about what probably broke. Contracts are compared on the behaviour a consumer
is shown — operations, obligations, invariants — and never on planner-defined metadata:
the built-in planner copies the whole project spec into every contract's metadata, so
comparing that would make every contract differ whenever any requirement anywhere
changed. A node's *shape* (owned paths, ports, kind) is not part of its promise, so
changing it makes that node stale and tells its consumers nothing.

Verification is a separate question with a separate answer: the gates are
whole-application and always re-run. Reusing an answer is never reusing a verdict, and
every plan says so in words.

**Measured, on a model-authored architecture.** For a two-requirement project — a todo
capability and a keyboard-accessibility constraint — the architect allocated
`req.a11y` to `web` alone and `req.todo` to `data`, `domain` and `web`: 67% of a dense
allocation, not 100%. Amending each in turn:

| Amendment | Stale | Reusable |
|---|---|---|
| `req.a11y` (UI only) | `app`, `web` | `data`, `domain` |
| `req.todo` (spans layers) | `app`, `data`, `domain`, `web` | — |

The second is not a failure of the mechanism: that requirement really is served by every
layer, so every layer really must be rebuilt. The first is the point — cost proportional
to the change, on an architecture nobody hand-tuned for the demonstration.

**Driven end to end against a real model.** `tests/test_change_locality_live.py`
builds the application, amends a requirement the domain layer does not serve, applies
the change, and runs again:

- `domain` **replayed** its remembered generation and passed every gate on its first
  attempt;
- `web`, which does serve that requirement, was written again;
- and the second pass re-ran lint, types, unit, properties, build and acceptance in full.

That last point is the one to keep hold of. A smaller stale set is not a smaller proof.

**The allocation is what decides the cost.** The deterministic planner gives every layer
every requirement, which is honest for a layered baseline — a feature really does cut
through UI, domain and data — and buys no change locality at all: every amendment stales
everything. Modularity under change is a property of the allocation, not of having
drawn boxes, so the architect is asked for the minimum allocation and told why.

### 10. Obligations are executed, not merely declared

A contract's proof obligations are compiled into a vitest suite against a pinned
`Operations` interface and run as their own gate, separate from the unit gate so the
evidence says which claim held. Suites, generator, and interface are all protected
generation inputs: they are compiled from the approved contract, so a worker able to edit
them could edit the claim it is being held to.

The interface lives at `packages/contracts/src/operations.ts` — inside the domain node's
ownership, because that is where it must be importable from. That makes it the one
protected input a worker could otherwise legally rewrite, so it carries an explicit path
rule. Being protected also scopes it out of `current_files`, so it is handed directly to
the task that must implement it and to no other: a worker told to satisfy a surface it
cannot see fails the typecheck for a reason it was never given.

The gate is skipped when no suite was scaffolded. Vitest passes over an empty directory,
and a passing check that checked nothing is the precise failure this design exists to
avoid — the same reason `ContractV2` refuses a contract whose only claims are unanchored,
and the same reason the obligation compiler refuses a `PROOF`-tier claim it can only
sample.

### 11. Evidence flows forward into the retry

A gate failure is recorded as a `rich.command-verification/v1` artifact holding the exact
command, exit status, and bounded stdout/stderr. That evidence is also fed into the next
attempt's prompt (`redact_diagnostics`, `PriorAttemptFailure`), because a retry that
cannot see why the last attempt failed is just another first attempt charged to the same
budget.

This does not weaken §8. What flows forward is an *independently observed command result*,
never a model's claim about itself; verification still runs out of process, and a worker
still cannot publish its own success. The direction matters: evidence may inform
generation, but generation may never become evidence.

It does raise a firewall question, since a compiler or test diagnostic can name files the
task does not own and quote their contents. A diagnostic line is disclosed only when every
path it names is either the task's own source or a generated test — those being the
approved requirements and acceptance scenarios rendered executable, which the prompt
already contains. Continuation lines such as code frames carry no path of their own and so
inherit the disclosability of the line that last named a file; without that rule a
sibling's source would pass through inside an indented context block. Withheld lines are
reported to the worker as a count, which is the signal it needs: a consumer broke, so
re-read the contract rather than guess at the consumer.

An acceptance failure is attributed to whoever owns what it exercised. The
browser runs at the composition root, whose owned paths hold nothing a browser
can see, so retrying the root cannot change the outcome — the first live build
spent three attempts (about $0.35 and seven minutes) regenerating a root shim
while the page it needed sat in the `web` node's source. The pack names the
page files a scenario opens (`exercised_pages`), ownership names their owners,
and the verified handler records them on the evidence and the verification
artifact (`attributed_node_ids`). The scheduler decides what that is worth
before any write: an upstream owner that finished and still has attempts is
**reopened** (`succeeded → ready`, event `task.reopened`) and everything
downstream of it — the root included — is **superseded** back to `pending`,
so the owner's next attempt reads the failed steps in its
`prior_attempt_failures` and the root's gates run again over the new source.
Owners with no attempts left withhold the retry outright
(`task.retry_withheld`); an attribution naming anything that is not a finished
upstream task is ignored and the ordinary retry applies, so a wrong attribution
can only cost what a plain retry costs. Nothing here touches evidence: the
reopened attempt is generated and verified exactly like any other.

### 12. What was verified is what can deploy

Before root verification, RICH records the deployment-source digest set. Verification
may write only declared outputs. After all gates, any source change fails the run except
Next's narrowly validated generated `next-env.d.ts` grammar.

RICH then creates a deterministic ZIP from exactly the deployable source set and stores
it as `source:release-snapshot`. The acceptance evidence includes that snapshot's SHA-256.

A preview request is allowed only for a completed run whose live source still exactly
matches that verified snapshot. Preview approval binds the source digest and deployment
parameters. Deployment extracts the immutable artifact into a new directory and uploads
that snapshot—not the mutable working tree.

Neon database branches and Vercel deployments are separate external side effects, use
lazy secret handles, have durable state, and support expiry/teardown. Approved upload
bytes are frozen before migration preparation. Migrations run from a disposable
extraction, but no generated Node process receives the database credential: trusted
Python code reads only bounded, convention-named UTF-8 SQL files and applies them through
`psycopg` with lock/statement timeouts and a digest journal. See the official
[Neon branch workflow](https://neon.com/docs/get-started-with-neon/workflow-primer) and
[Vercel API integration guidance](https://vercel.com/docs/integrations/create-integration/vercel-api-integrations).

**Getting the software out.** Two exits hand the customer exactly what was
verified and nothing else. `GET /v1/runs/{id}/release` streams the release
snapshot with its digest in a header. `POST /v1/runs/{id}/repository-pushes`
(`rich push-repository`) pushes that same stored snapshot — never the working
tree, so drift is impossible — as one deterministic commit (`repository.py`: a
fixed author, the run's finish time as both dates, the digest in the message),
landing on top of whatever the branch already holds, so a repository
accumulates one commit per verified release and a re-push of the same run is
a no-op. The token comes from the closed secret-handle map (`github.token` →
`GITHUB_TOKEN`) and reaches `git` only through `GIT_ASKPASS`; it is never in
argv, in the repository's configuration, or in an error message. Only
`https://` and `file://` remotes are accepted. There is no approval gate: the
repository is the customer's own, and the push, like the download, is theirs
to take. The receipt is an artifact on the run and a `repository.pushed` event.

## Trust boundaries

| Component | Trusted for | Not trusted for |
|---|---|---|
| Interview/compiler/planner | schema and graph construction | proving generated behavior |
| Approval/store | authority, revision identity, durable ordering | code correctness |
| Target pack | toolchain, tests, ownership, release shape | arbitrary domains not modeled by the pack |
| Model provider | proposing source within owned paths | approvals, budgets, evidence, release status |
| Coding worker | parsing and bounded file mutation | verification |
| Bubblewrap runner | observed isolated process results | completeness beyond the approved executable oracle |
| Scheduler | evidence-policy enforcement and status commit | inventing missing evidence |
| Preview orchestrator | exact approved snapshot deployment | authorizing a new source digest |

The operator and the installed RICH code/tool bundles are part of the trusted computing
base. Bubblewrap isolates generated code from the host; it does not defend against an
operator who modifies RICH itself or replaces the validated tool installation.

## Durable data model

`RichStore` uses SQLite for metadata and a SHA-256 content-addressed artifact directory.
It stores:

- projects and optimistic revisions;
- approvals and claims;
- runs and compiled tasks;
- append-only run/task events;
- task attempts and evidence/artifact links;
- prepared/committed/recovered source transactions and their CAS journals;
- provider reservations and settlements;
- execution leases and fencing tokens;
- idempotency claims/responses;
- preview requests, resources, expiry, and teardown state.

Filesystem source is not the authority for completion. Durable state plus content-addressed
artifacts is. A workspace can be reconstructed from the scaffold/release manifests and
artifact bytes.

## Operating the current vertical

### Prerequisites

- Linux with unprivileged user namespaces and Bubblewrap;
- Python 3.10+;
- exact Node 22.23.2;
- exact pnpm 10.34.5 present in the local Corepack cache;
- a model route: an `ANTHROPIC_API_KEY` (the `api` route) or an existing `claude`
  login (the `claude-code` route). Neither is a fallback for the other.

`rich doctor` checks coarse host availability. Runtime construction performs the exact
Node/pnpm identity checks and fails closed on drift.

### Local Canvas

```bash
python -m pip install -e '.[test]'
npm --prefix web ci
npm --prefix web run build
rich serve
```

The Canvas runs on `http://127.0.0.1:8767` by default and serves the JSON API under `/v1`.
State defaults to `.rich/state`; generated API workspaces default to
`.rich/workspaces`. `--route` chooses `claude-code` (an existing login), `api`
(`ANTHROPIC_API_KEY`) or `none` — a real mode, not a missing credential: the deterministic
planner and the fixed questions, and the server says so at startup.

The canvas keeps only a pointer to the project and who is deciding; everything the
project holds comes back from `GET /v1/projects/{id}/state` in the shapes the submission
calls return, so a reload or a switch lands on the durable truth. Building is one action
over three durable authority boundaries — prepare binds the budget, scaffold writes the
frozen tree, execute takes the fenced lease — with one dollar figure deciding the whole
budget and the derived dimensions shown. `GET /v1/runs/{id}/usage` sums settled model
usage from the events the way a restart recovers the budget, so the meter is never
optimistic; `GET /v1/runs/{id}/timeline` serves the lines `rich logs` prints, from the one
formatter. A settled run is never a dead end: the canvas shows the gate that failed, the
failed steps in the words that were approved, and the three ways forward. The verified
release ZIP streams from `GET /v1/runs/{id}/release` with its digest in a header.

### CLI lifecycle

The CLI mirrors the state machine:

```text
project-create
interview-submit
approve
architecture-propose
approve
run-prepare
scaffold
run-execute
preview-request
approve
preview-deploy
preview-destroy
```

Two commands sit outside that line because they act on a run already in
flight:

```text
rebuild-node --project P --node domain   # forget one node's remembered
                                         # generation; siblings still replay
cancel-run RUN_ID                        # stop at the next checkpoint
logs RUN_ID --follow                     # watch it happen
```

Commands return JSON so IDs can be captured by an operator or a higher-level workflow.
Use `rich events RUN_ID` to inspect the durable proof/recovery trail.

### API rules

- API version: `/v1`.
- Bind default: loopback only.
- Mutations require `Idempotency-Key`.
- Reusing a key with different request content is rejected.
- Request bodies are bounded.
- Host/origin checks reject cross-site and non-local control requests.
- Run execution is backgrounded in the Canvas; status comes from durable run state.

## Validation

The offline suite never calls a model or deployment provider:

```bash
ruff check .
python -m pytest
```

Live host/toolchain tests are opt-in:

```bash
python -m pytest --run-live tests/test_executor.py
python -m pytest --run-live \
  --basetemp=.rich/live-tests \
  tests/test_public_runtime_live.py
```

The public-runtime live test creates a fresh approved spec/architecture/scaffold, installs
the frozen dependency graph and Chromium inside Bubblewrap, and runs the six independent
gates. It does not use a model credential.

The Canvas frontend is checked independently:

```bash
npm --prefix web run build
npm --prefix web run typecheck
```

## What comes next

The program that takes this kernel to a finished product -- three releases, twenty-two
milestones, one customer scenario as the definition of done -- is `docs/program.md`,
and the live tracker is `docs/board.html`, rendered from `docs/board/cards/`. Change
compilation, once item five of a roadmap here, is §9 above; the rest of that roadmap is
ordered there rather than listed here twice.

The invariant through all of it is unchanged: authority is explicit, generation is
bounded, verification is independent, and only the exact verified artifact may advance.
