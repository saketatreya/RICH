---
id: decision-the-unwired-openai-adapter
title: "The unwired OpenAI adapter"
kind: decision
status: decision
track: core
verdict: kept
milestone: pre
---

A seam with one implementation is not a seam. The conformance suite that proves the
claim now exists (`tests/test_provider_conformance.py`, seven rules, three adapters,
shipped cec3beb), so the adapter stays as the second implementation — wired to nothing,
never a fallback.
