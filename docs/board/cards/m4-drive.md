---
id: m4-drive
title: 'Drive: amend a requirement, see the cost, rebuild only that'
status: doing
track: P
milestone: M4
tag: drive
started: '2026-08-30'
---

Approve a spec and design, build; amend one requirement in the conversation, approve, redraft (the untouched layers carried forward), approve; the cost shows 2 of N stale; Apply and build; the untouched components replay from memo. Live, on the claude-code route.

Four live runs so far. Run 1 refused the prompt (29,332 bytes over a 24,000 ceiling) — fixed by a 48 KB budget and prior failures fitted to it. Run 2's worker ignored the scaffolded pages — fixed by naming them as deliverable one and by a placeholder that says who replaces it. Run 3 wrote both pages and passed accessibility, then failed the workflow scenario at step 4, "Expect to see the text 'approval'". Run 4 never reached a model: the subscription's five-hour window had closed, and the run burned four attempts on HTTP 429 in six seconds ([[m3-quota-does-not-burn-attempts]]). Run 5 got further than any before it — domain, web and app all built, the owner retry fired twice and worked both times — and then found two more, both fixed in `98d4a0d` ([[m4-prompt-budget-refused-before-sending]]).

**Run 6 built the example on its first attempt** — three components, every gate green, no
reopen — which is what the page fix bought ([[m4-first-attempt-never-saw-the-pages]]).
It then failed on the step the milestone is named for: the cost was not shown, because
`ChangeCost` computed nothing until asked, so a customer who had approved a redraft was
offered Build beside a panel naming a cost it had never computed (`15acad9`).

**Runs 8 and 9 drove the architect instead of the fallback.** The architect allocates per
requirement — `domain` and `data` serve only `req.workflow`, `web` and `app` serve both —
so the amendment the drive makes can leave something untouched, which the fallback's shape
never could. Run 8 stalled because the two routes to an approved design said different
things (`a39ad91`). Run 9 got four components through the architect and built two of them,
then `web` burned two attempts on a forty-two-token reservation overage
([[m4-output-reservation-killed-work-already-done]]).
