---
id: m3-quota-does-not-burn-attempts
title: A route that says 'come back later' does not spend the attempts meant for bad code
status: done
track: P
milestone: M3
started: '2026-08-30'
sha: 3a518e8
finished: '2026-08-30'
---

The fourth M4 live drive died in 42 seconds: the operator's Claude subscription hit its five-hour session limit, the CLI route raised ProviderFailure(retryable=True, HTTP 429) four times, and the scheduler spent every one of the web task's four attempts on it -- `attempt 2 in 0.0s`, then 3, then 4 -- before failing the run and blocking `app`. The attempts exist for generation quality; a route that refuses to answer is not a bad attempt. The customer read `handler raised ProviderFailure` and lost a run whose `domain` component had already passed every gate.
