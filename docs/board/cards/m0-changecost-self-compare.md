---
id: m0-changecost-self-compare
title: ChangeCost stops comparing a revision with itself
status: done
track: P
tag: canvas
milestone: M0
order: 11
sha: 56117ce
finished: '2026-08-29'
---

Wired to `fromSpec === toSpec`, so the plan was always empty and the warning never
fired. Hidden until two approved revisions exist; real wiring is M4.
