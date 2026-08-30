---
id: m7-web-owns-too-much-for-one-attempt
title: The reopened frontend component is too big for one attempt on the CLI route
status: next
track: E
milestone: M13
---

M7's second live proof (2026-08-30, 1504s, $1.96, 99,753 output tokens): `data` and `domain` succeeded, `web` was correctly reopened when acceptance failed, and both retries then died on the route rather than on the work — attempt 2 on "provider-reported usage exceeded the reserved maximum" and attempt 3 on the 600s timeout. `web` owns all of `apps/web` plus `packages/ui`: the whole frontend tier of a persisting application, in one bounded generation the CLI route cannot cap before the fact. This is an argument for M13: capability-shaped components are smaller than layer-shaped ones, and the reservation is sized per component.
