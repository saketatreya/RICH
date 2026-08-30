---
id: m4-cost-shown-without-asking
title: The cost of an amendment appears without being asked for
status: done
track: P
milestone: M4
sha: 15acad9
finished: '2026-08-30'
---

The sixth M4 live drive failed on the step the milestone is named for: "the cost is shown before any money moves". It was not shown. `ChangeCost` computed nothing until someone pressed "Compute the cost", so a customer who had amended a requirement and approved a redrafted architecture was offered "Apply and build" beside a panel that named a cost it had never computed — she could commit the money without once being told what it bought, and nothing on screen suggested she was missing anything. Planning is a read: it compares two approved revisions, calls no model and spends nothing, so it now runs as soon as there are two designs to compare.
