---
id: m3-park-a-run-the-route-refused
title: A build the route refused pauses where it stopped, instead of failing
status: next
track: P
milestone: M3
---

The half of the quota design that is not built yet. Today a run whose route stays unavailable past its two waits fails: legibly, with every verified component memoized, so Build again is cheap and nothing verified is lost. The designed behaviour is better — park the run at a non-terminal status that holds no lease, keeps every succeeded task, and resumes at the component it stopped on, so Maya reads 'pausing the build so nothing is lost' rather than a failure she has to restart. It needs a run status, a resume route, a canvas panel and a drive step, which is why it is its own card rather than a bigger commit. The full design is in the session's plan: withdraw → wait → park → resume, with the park check placed after the cancellation read and before the ready scan.
