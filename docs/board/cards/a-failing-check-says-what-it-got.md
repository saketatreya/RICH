---
id: a-failing-check-says-what-it-got
title: "A failing check says what it got"
status: done
track: E
tag: engine
milestone: pre
order: 12
sha: 344241c
---

Every generated assertion collapsed to a boolean before asserting, so a failure read
"expected false to be true". The retry loop was working perfectly and being handed a
message with no information in it.
