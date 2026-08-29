---
id: two-vulnerabilities-in-the-old-api
title: "Two vulnerabilities in the old API"
status: done
track: E
tag: engine
milestone: pre
order: 34
sha: 5b1b042
---

Arbitrary-directory source read via path traversal, and no Host check at all — a DNS-
rebinding hole. Both fixed, then the surface was deleted outright.
