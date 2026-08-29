---
id: the-verifier-accepts-idiomatic-typescript
title: "The verifier accepts idiomatic TypeScript"
status: done
track: E
tag: engine
milestone: pre
order: 10
sha: 6fc6019
---

`_input` is how TypeScript says "deliberately unused", and the pinned interface makes it
common. The lint gate was failing correct code for it.
