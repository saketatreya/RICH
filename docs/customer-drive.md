# The customer drive, step by step

`docs/program.md` makes one scenario the definition of done: Maya, a product
lead with no terminal habits, takes software from a sentence to production and
back for an amendment. This page tracks that scenario against what the product
does today. It is updated when a milestone lands; the board
(`docs/board.html`) carries the same facts as cards. Nothing here is a plan —
a step is marked held only when a drive under `web/drive/` or a live test has
done it.

| Step | What Maya does | Holds today | Proven by | Waits on |
|---|---|---|---|---|
| 1 | `docker run` one line; opens `localhost:8767`; `doctor` is green | Image builds; the canvas is served from the wheel; `rich doctor` inside the container is green with `docker/seccomp.json` | CI `image` and `package` jobs (every push) | a tag (M12) for the published image |
| 2 | Describes the tracker in prose; RICH asks three questions; she answers in prose | The model interviewer asks and writes the draft; without a model route the fixed questions do | `tests/test_interviewer_live.py`; drive M2 | — |
| 3 | Readable spec with scenarios as editable sentences; approves | Requirements and scenarios render as sentences with dropdowns; the canvas and the Playwright titles say the same words; a project needs only a name, and no id, digest, revision counter or schema number is on screen | drive M2; `web/src/components/intent/steps.test.ts` against the shared fixture; the M12 re-audit pass (`a52118e`) | — |
| 4 | Architecture as a graph of capabilities; drags a requirement between them; asks for a contract; approves | Graph of components with contracts, read as behaviour; redraft in prose; approval binds the revision | drive M2/M3 | M13 (capabilities, not layers) and M5 (editing the graph directly) |
| 5 | Build with $15; four components at once; nodes light up; cost climbs; a failed gate reads in a sentence; it retries and passes | One Build; nodes take task colours; cost meter; sentences in the timeline; a failed browser step names itself; the component that owns the page is reopened and retried; exhausted owners are named with one rebuild action | drive M3 (live build, first run); offline tests for the owner retry; the first M4 live run (2026-08-30) exercised the reopen for real and found two more things, both fixed in `355de5b`: the worker was never told which page a scenario opens, and the feedback it was then shown was too large for its prompt | M14 for "at once" (single worker today); the M4 drive running again |
| 6 | Assurance per requirement and gate; preview URL; signs in; data persists; Slack is proven against fixtures | Assurance lists gates per requirement; preview is digest-bound and approval-gated | drive M3; `tests/test_preview.py` (offline) | M7 persistence (engine/m7 in progress), M8 identity, M9 integrations; live preview needs Neon/Vercel credentials |
| 7 | Promote to production; rollback point; downloads the ZIP; pushes to GitHub | The ZIP downloads with its digest; the push lands the verified snapshot as one deterministic commit | drive M6 (CI on every push) | M16 promotion with rollback |
| 8 | A week later: "tasks can be snoozed" — cost 2 of 5; rebuild; every gate re-runs; a bad migration rolls back | Amend in the conversation; the architect carries untouched contracts forward byte for byte; the cost is shown before committing; Apply and build | `tests/test_change_locality_live.py`; offline redraft tests | drive M4 (live, two builds); M16 for rollback |

**Paradigm checks** (the same contracts through other packs) wait on M10's
`TargetPack` protocol and release 2.2's packs.

**What only the owner can supply:** a license (M11), Neon and Vercel
credentials for the live preview (M6), a GitHub token where `rich serve` runs
for pushes over https.
