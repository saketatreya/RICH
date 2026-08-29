---
id: a-failed-attempt-says-why
title: "A failed attempt says why"
status: done
track: E
tag: engine
milestone: pre
order: 3
sha: c6895d4
---

"handler raised ProviderFailure" with the reason discarded — the event recorded the
reservation, the usage and the retryable flag, but not what went wrong. Now a bounded
reason, capped so a chatty upstream cannot push a response body into the event stream.
