---
id: m3-toolchain-drift-refused
title: A drifted toolchain is refused before acceptance, by name
status: done
track: E
milestone: M3
tag: engine
sha: f50e3c6
finished: '2026-08-29'
---

Found by the M3 drive in twelve seconds once it failed fast: the host's Node had moved to 22.23.2 and the run died after acceptance as SandboxUnavailable. The pin moves (seven places, one commit); the probe resolves the toolchain before a run is accepted and names the drift.
