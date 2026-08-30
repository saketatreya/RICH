---
id: m3-canvas-budget-assumes-one-route
title: The canvas sizes a budget from one route's numbers whichever route is running
status: next
track: P
milestone: M3
---

`budgetPlan` in ControlPlane.tsx derives every dimension of a budget from the dollar figure using constants hardcoded to the HTTP route — 32k input, 8k output, 120s per attempt — and says so in its own comment: "the way the run engine sizes an attempt". On the CLI route an attempt reserves 48k input, 64k output and 600s, so a $10 budget derives an output ceiling roughly a fifth of what twenty attempts would need and a time ceiling a fifth of theirs. The dollar figure the customer typed is not what binds first; a derived dimension she was shown but did not choose is. Same class as the defect where `execution.py` never passed the route's limits to the engine (98d4a0d): route-specific sizing living somewhere that does not know the route. The fix is to serve the per-attempt shape from the API rather than restate it in the canvas.
