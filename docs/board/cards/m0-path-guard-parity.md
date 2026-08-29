---
id: m0-path-guard-parity
title: "The models path guard has the same rules"
status: doing
track: E
tag: engine
milestone: M0
order: 5
started: 2026-08-29
---

The deliberate own-copy lacked the null-byte, trailing-slash and 255-byte rules
`paths.py` enforces, so "same rules" was not true.
