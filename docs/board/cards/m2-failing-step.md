---
id: m2-failing-step
title: A failing scenario points at its step
status: next
track: P
milestone: M2
tag: engine
---

Each compiled oracle step becomes a named `test.step`; the protected reporter emits a second `RICH_ACCEPTANCE_FAILURES` line the engine parses leniently on a *failed* acceptance only; Assurance highlights the failing step in the same words the canvas showed. The strict coverage parser is untouched.
