# Changelog

All notable changes to RICH are recorded here. Versions follow
[semantic versioning](https://semver.org/spec/v2.0.0.html).

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

[1.0.0]: https://github.com/
