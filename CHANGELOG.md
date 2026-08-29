# Changelog

All notable changes to RICH are recorded here. Versions follow
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Toward 2.0 — the release that builds real software. `1.0.0` was tagged on a
tree a wheel could not install, so the program's releases are numbered 2.0,
2.1 and 2.2 rather than moving a tag; the program itself is `docs/program.md`.

### Added

- **An acceptance failure reopens the task that owns the page.** The browser
  runs at the composition root, which owns nothing a browser can see; the pack
  now names the pages a scenario opens, the evidence names their owners, and
  the scheduler reopens the owner — its next attempt reads the failed steps —
  and runs everything downstream again. Owners out of attempts withhold the
  retry instead of spending it on the root. Step messages lose their ANSI
  colour codes.
- **No ids on screen.** A project is created with a name alone (the server
  mints the id; `project_id` is optional on `POST /v1/projects`); the project
  list, banners, footer and stage rail show names and dates, not ids or
  revision counters; the specification panel shows its version and when it
  was written instead of a revision id, a schema version and a hard-coded
  "Coverage 100%"; approval ids and store schema numbers leave the page; the
  "test id" way of finding an element leaves the step editor; "Compile product
  specification" is "Write the specification". Found by the M12 re-audit.
- **A seccomp profile for the image.** `docker/seccomp.json` is Docker's
  default profile with only the namespace and mount syscalls Bubblewrap uses
  admitted; CI runs the container with it and requires `rich doctor` green.
- **A failed run names the component that used every attempt** and offers
  "Rebuild ‹node› and build again" as one act, beside "Build again as is".
- **The README carries the customer's path**: install, first build, getting
  it out, amending.
- **A container image.** `docker build -t rich .` produces an Ubuntu 24.04
  image with Bubblewrap, the pinned Node and pnpm (in the Corepack cache the
  executor reads), Chromium's system libraries and the wheel; state lives on
  a volume, the port is published to the host's loopback, and inside the
  container `rich serve --host 0.0.0.0 --published-on-loopback` enforces the
  Host and Origin checks it could leave optional on loopback. CI runs the
  image and asks `rich doctor` whether Bubblewrap works under Docker's default
  seccomp profile and with it unconfined.
- **The wheel carries the canvas.** `python tools/build_wheel.py` builds the
  canvas, packages it as data inside `richbuild/canvas`, and leaves the
  checkout clean; an installed `rich serve` needs no Node toolchain to show
  the product, and `rich doctor` and the serve banner say which canvas is
  served (`repo`, `bundled`, or `missing`). CI installs the wheel into an
  empty venv and fetches the canvas from it. Version is `2.0.0.dev0`.
- **Push the verified snapshot to a repository.** `POST
  /v1/runs/{id}/repository-pushes` and `rich push-repository` commit the
  run's stored release snapshot — never the working tree — as one
  deterministic commit on top of the branch, create the GitHub repository on
  request, and record a receipt on the run; the canvas offers it beside the
  ZIP download. The token reaches `git` only through `GIT_ASKPASS`.
- **The example scenarios open their requirement's page**, where the generated
  application puts a requirement's form, instead of the home page that only
  lists capabilities; the interviewer's prompt says the same.
- **A project you can return to.** `GET /v1/projects/{id}/state` restores the
  latest spec and architecture with their approvals, the runs, the latest run's
  compiled plan and scaffold, the previews and the interview draft in one
  answer; the canvas keeps only a pointer to the project and who is deciding.
- **The interview draft lives on the server** (`GET`/`PUT
  /v1/projects/{id}/interview`), autosaved shortly after each edit with an
  optimistic revision, so a reload never loses a word and two tabs never
  silently overwrite each other.
- **Download the release ZIP.** `GET /v1/runs/{id}/release` streams the exact
  verified snapshot with its digest in a header; a succeeded run shows the link.
- **A rendered, measured delivery board.** Cards are files under
  `docs/board/cards/`; `tools/board.py` validates, renders and records a real
  verification run, and the suite fails when the board misreports.
- **Browser drives.** `web/drive/` holds each milestone's acceptance drive,
  run against a live `rich serve`.

### Fixed

- **A model worker could self-certify the property gate**: the forbidden
  evidence set listed five kinds while the run engine had six.
- **A wheel could not import `richbuild`**: `pyproject.toml` listed packages by
  hand and omitted `richbuild.models`. CI now builds, installs and imports the
  wheel.
- **The canvas preview request always failed**: it sent the workspace-relative
  form string as the source directory. It sends the scaffold destination the
  run's events name, and deploys the preview it just had approved.
- **ChangeCost compared each revision with itself**, so the plan was always
  empty. It leaves the page until it is wired between approved pairs.
- Two canonical encodings became one; duplicated helpers (`_all_events`,
  `_fsync_directory`, the providers' HTTP transport, owned-path pass-throughs)
  became one definition each.

### Changed

- The canvas' CSS namespace is `plane-` and its error class is `ApiError`;
  nothing is named for a version that no longer exists.
- `docs/architecture.md` says six gates, two routes, and numbers its
  invariants in order; `docs/spec.md`, which described the deleted v1 engine,
  is gone.

## [1.0.0] — 2026-08-27

The first release of RICH as one product rather than two systems sharing a
repository.

### Added

- **Contract obligations are executed.** A contract's proof obligations compile
  to a Vitest suite against a pinned `Operations` interface and run as their own
  gate. The suite, the generator and the interface are protected inputs, so a
  worker cannot edit the claim it is being held to. No suite scaffolded means no
  gate — a property run over an empty directory would pass without checking
  anything.
- **Generation memoization.** Keyed on the exact request — both prompts,
  provider, model and response schema — so an unchanged task is not paid for
  twice. A retry carries its predecessor's failures in the prompt, so it keys
  differently and can never replay the answer that just failed. Reuse is
  revalidated through the same parser, transaction and gates: a memo skips
  asking, never checking.
- **Rebuild one node.** `rich rebuild-node` marks a single architecture node
  stale so the next run regenerates it while its siblings replay from memo.
  Scoped per project, reachable from the control plane, API, CLI and inspector.
- **Durable cancellation.** `rich cancel-run` asks a run to stop at its next
  checkpoint. Recorded durably, so the request reaches whichever process holds
  the run rather than only the one that was asked.
- **Retry feedback.** A failed gate's observed output is fed into the next
  attempt, redacted to what the failing task may read: a diagnostic naming a
  file the task does not own is withheld and reported as a count.
- **Preview from the canvas.** Requesting a preview records an approval bound to
  the verified source digest; deploying re-checks it.
- **The architecture as a graph**, with live per-node task status and per-node
  rebuild.
- **An adaptive interview.** It asks the questions your project raises and says
  why, instead of rejecting an incomplete fixed form.
- **One server.** The API and the canvas share a port.

### Changed

- **The whole product is one package**, `richbuild`, with one command, `rich`,
  and one HTTP API under `/v1`.
- The trusted model policy is `anthropic/claude-sonnet-5`, reached by two
  explicitly chosen routes — an API key or an existing `claude` login — which
  are never fallbacks for one another.
- The architect no longer rejects a design over bookkeeping: bounds and
  character sets are derived from the examples the model supplies, an
  obligation it cannot express is dropped rather than fatal, and a proposal that
  still cannot be assembled degrades to the deterministic baseline instead of
  raising. First-attempt assembly measured at 6/6, against a 0/6 baseline.
- `models` is a package of six modules by subject rather than one 3,900-line
  file, re-exported under one import name.
- One canonical JSON encoder, one relative-path guard, one ownership check, one
  set of shared provider helpers.
- Supported Python is 3.10 through 3.14, each verified in CI.

### Fixed

- **Arbitrary-directory source read.** The node-artifact endpoint joined the
  build root with an unsanitized query parameter, so `../..` walked it out of
  the build directory and read `src/*.py` from any project on disk.
- **Missing DNS-rebinding protection.** The legacy API performed no `Host`
  check, so a page whose domain resolved to loopback was same-origin to the
  browser and could drive builds. Both surfaces now share one validator.
- **Execution status reported process-local state** while the guarantee lived in
  SQLite, so a run executing under another process read as idle.
- **Approval gates were unvalidated strings.** A typo would have opened an
  approval at a gate nothing checks — granted in appearance, authorizing
  nothing. Gates are validated at the store boundary.
- **Canonical JSON had drifted.** Of four definitions, the store's permitted
  `NaN` and `Infinity`, which `json.dumps` emits as bare tokens that are not
  JSON.
- **Workspace confinement guarded only the HTTP layer**, so the control plane
  and CLI accepted any destination. The boundary now belongs to the control
  plane.

### Removed

- The v1 recursive build engine, its skills and backends, its canned demos, its
  HTTP surface, its half of the single-page app, and the v1-canvas importer that
  read a format nothing produces any more.

[Unreleased]: https://github.com/saketatreya/RICH/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/saketatreya/RICH/releases/tag/v1.0.0
