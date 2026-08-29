---
id: m7-spike
title: 'Spike: PGlite runs inside the gates'
status: done
track: E
milestone: M7
sha: 13c48d2
finished: '2026-08-29'
---

PASSED twice (88.9 s, 75.8 s cold). Two deltas only: .rich/runtime/db writable and RICH_DATABASE_DIR in the env. Probe 1.4 s, node VmPeak 7.2 GiB; next build 9.8 s emitting import() externals (not require); fill→click→reload→assert 3.5 s; the probe reads the row back. Findings: a plain next build already peaks 16 kB under the 16 GiB ceiling; the probe must resolve pglite through packages/db; the runner needs a per-gate environment seam; pack version must bump to 1.4.0 with the scaffold change.
