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

Four live runs so far. Run 1 refused the prompt (29,332 bytes over a 24,000 ceiling) — fixed by a 48 KB budget and prior failures fitted to it. Run 2's worker ignored the scaffolded pages — fixed by naming them as deliverable one and by a placeholder that says who replaces it. Run 3 wrote both pages and passed accessibility, then failed the workflow scenario at step 4, "Expect to see the text 'approval'". Run 4 never reached a model: the subscription's five-hour window had closed, and the run burned four attempts on HTTP 429 in six seconds ([[m3-quota-does-not-burn-attempts]]).
