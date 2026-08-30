---
id: m4-prompt-budget-refused-before-sending
title: A prompt the byte budget admitted was refused before it was sent
status: done
track: P
milestone: M4
sha: 98d4a0d
finished: '2026-08-30'
---

The fifth M4 live drive, and found only because the failure summary now carries the provider's own sentence: `web` burned attempts 3 and 4 in the same second on "prompt UTF-8 byte upper bound exceeds input token reservation". `CodingLimits` checked `max_prompt_bytes > max_input_tokens * 4` under a message saying "cannot exceed max_input_tokens", while `ModelRequest` enforces the strict comparison — four times looser than the rule it guards. Unifying the two tracks' prompt measurements at 48,000 bytes made the gap reachable. Two defects fixed: the guard is now the provider's comparison with the reservation and its priced ceiling sized to match, and `execution.py` now passes the route's own limits to the engine — until now every build a customer ran used the HTTP route's bounds whichever route it was on.
