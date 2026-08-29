---
id: change-locality-proven-live-green
title: "Change locality, proven live · green"
status: done
track: E
tag: engine
milestone: pre
order: 8
sha: 6fc6019
---

A standing test now: build, amend a requirement the domain layer does not serve,
rebuild. Domain *replayed* its memo and passed every gate on its first attempt; web,
which does serve it, was rewritten. Nine real defects came out of getting there.
