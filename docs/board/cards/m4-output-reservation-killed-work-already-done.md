---
id: m4-output-reservation-killed-work-already-done
title: The output reservation discarded two attempts that had done the work
status: done
track: P
milestone: M4
sha: e341ffd
finished: '2026-08-30'
---

The ninth M4 live drive: `data` and `domain` both built (each fixing its own property-gate failure on retry), then `web` burned two of its four attempts on "provider-reported usage exceeded the reserved maximum" and the run died. The numbers: reserved 24,000 output tokens, settled 24,042, then 26,437. Cost was $0.29 of a $1.00 ceiling, input 19,710 of 48,000, time 234s of 600s — output was the only binding dimension, and the first overage was forty-two tokens. This route cannot cap output before the fact, which is the whole reason its limits exist, so a reservation the model lands just outside is not a safety property but a way to throw away work that was done. What bounds an attempt here is the dollar ceiling. The token figure now sits at the model's own ceiling.
