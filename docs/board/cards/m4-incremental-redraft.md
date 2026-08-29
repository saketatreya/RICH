---
id: m4-incremental-redraft
title: A redraft keeps untouched contracts byte for byte
status: done
track: E
milestone: M4
tag: engine
sha: a9c7b73
finished: '2026-08-29'
---

The architect is handed the last approved design; a component the amendment does not touch comes back `unchanged` and the compiler carries its previous Contract forward exactly — refusing when it did not exist, its allocation moved, or a requirement it serves changed. Without this, every redraft staled every consumer.
