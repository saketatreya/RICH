---
id: m2-interview-turn
title: One bounded model call per interview turn
status: next
track: P
milestone: M2
tag: engine
---

`propose_interview` follows `propose_architecture`'s discipline: reserve through the gateway, two attempts with the validator's own rejection as repair, never raise. The outcome is complete, questions, or partial with the rejections carried; with no model route the deterministic questions answer, tagged `form-fallback`. `POST /v1/projects/{id}/interview-turns`.
