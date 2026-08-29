---
id: the-architect-was-unreachable-from-the-product
title: "The architect was unreachable from the product"
status: done
track: E
tag: engine
milestone: pre
order: 1
sha: 9188357
---

Driving it as a user: create, interview, approve, *Draft* → `source: planner` in 17ms.
No model call. The feature measured at 6/6 was not wired into the only surface anyone
uses — a regression from retiring the old canvas, which did wire it.
