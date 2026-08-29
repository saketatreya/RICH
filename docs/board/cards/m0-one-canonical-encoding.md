---
id: m0-one-canonical-encoding
title: "One canonical encoding, actually"
status: doing
track: E
tag: engine
milestone: M0
order: 2
started: 2026-08-29
---

`providers.canonical_request_bytes` re-implements `canonical.canonical_json_bytes`
without the trailing newline. First list what persists those bytes; then one definition.
