---
id: m3-cost-meter
title: Money is visible while it is being spent
status: done
track: P
milestone: M3
tag: api
sha: 60f8a38
finished: '2026-08-29'
---

`GET /v1/runs/{id}/usage` sums settled model usage from the durable events — `recover_model_usage` already reconstitutes it for restarts — against the run's budget. The canvas shows spent / budget, attempts and tokens, live.
