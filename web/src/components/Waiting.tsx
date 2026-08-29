import { useEffect, useState } from 'react'

/**
 * How long you have been waiting, and why it is taking that long.
 *
 * Some steps here are slow for a reason that is not obvious from a spinner:
 * drafting an architecture is one bounded model call and takes minutes, and a
 * button that says "Designing…" for two of them is indistinguishable from a
 * button that has hung. The count is not decoration — it is the difference
 * between waiting and wondering whether to reload.
 *
 * No estimate is shown, because RICH does not have one. A typical range is
 * honest; a progress bar over an unknown duration is not.
 */
export default function Waiting({
  since,
  what,
  typical,
}: {
  /** Epoch milliseconds the action started. */
  since: number
  what: string
  /** A truthful range, e.g. "usually 1–3 minutes". */
  typical?: string
}) {
  const [elapsed, setElapsed] = useState(() => Date.now() - since)

  useEffect(() => {
    setElapsed(Date.now() - since)
    const tick = window.setInterval(() => setElapsed(Date.now() - since), 1000)
    return () => window.clearInterval(tick)
  }, [since])

  const seconds = Math.max(0, Math.round(elapsed / 1000))
  const shown =
    seconds < 60
      ? `${seconds}s`
      : `${Math.floor(seconds / 60)}m ${String(seconds % 60).padStart(2, '0')}s`

  return (
    <p className="plane-waiting" role="status" aria-live="polite">
      <span className="plane-waiting-pulse" aria-hidden="true" />
      <span>
        {what} · <b>{shown}</b>
        {typical ? <small> {typical}</small> : null}
      </span>
    </p>
  )
}
