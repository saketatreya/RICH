---
id: decision-models-keeps-its-own-path-check
title: "`models` keeps its own path check"
kind: decision
status: decision
track: core
verdict: kept
milestone: pre
---

An explicit layering rule says it imports no sibling. Weakening that to fit a refactor
was the wrong trade, so the duplication is intentional and says so in the code.
