---
id: m7-live-proof
title: 'The live proof: a real model builds a todo list and the row outlives the reload'
status: doing
track: E
milestone: M7
started: '2026-08-30'
---

M7's exit is not the code, it is the run. `tests/test_persistence_live.py` asserts that a real model over the CLI route builds a todo list whose data component owns a schema, a migration, operations and a page; that the run succeeded with exact acceptance coverage; that the probe counted at least one row after the browser ran open_requirement → fill → click → assert_visible → reload → assert_visible; that the acceptance evidence carries the migration digest set the preview will be held to; and that the data component's property suite ran against the in-sandbox database.

**First run, 2026-08-30: failed, and the failure is the milestone's best evidence.** 657s,
8 model attempts, $0.86. `data`, `domain` and `web` all succeeded; `app` failed three
times. Told only "never import a database driver", the web worker kept the todos in a
`globalThis` array. `next start` is one long-lived process, so the array survived
`page.reload()`, every browser step passed, and the trusted probe — reading a database
whose tables were all empty — failed the run. The invariant held exactly as designed.
Two defects it exposed are fixed in faa4799: the prompt forbade the mechanism and not
the behaviour, and a probe failure named no owner, so the root was regenerated three
times while the component that held the array was never reopened.

**Second run, 2026-08-30: failed differently, and the fixes are visible in it.** 1504s,
6 model attempts, $1.96. The web worker wrote a *stateless* operations const, the stray
lint warning that came with the invented store is gone, acceptance failed honestly, and
the owner-retry reopened `web` rather than regenerating the root. Both retries then died
on the route rather than on the work — the per-attempt reservation and the 600s timeout
([[m7-web-owns-too-much-for-one-attempt]]).
