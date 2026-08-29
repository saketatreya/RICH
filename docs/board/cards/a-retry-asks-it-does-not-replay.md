---
id: a-retry-asks-it-does-not-replay
title: "A retry asks, it does not replay"
status: done
track: E
tag: engine
milestone: pre
order: 16
sha: 00992ae
---

Fixing the key made every attempt replay the same rejected answer — three identical gate
failures without ever asking the model.
