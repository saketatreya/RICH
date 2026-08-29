import { useEffect, useState } from 'react'

import { type Run, type RunEvent, type RunTimeline, type RunUsage, api } from '../lib/api'
import { shortId, statusClass } from '../lib/format'

/**
 * A run while it happens, and what to do when it stops.
 *
 * Three things a person watching a build needs and the raw event feed did not
 * give them: how much has been spent against the ceiling they set, what is
 * happening in plain language, and -- when it fails -- why, and what they can
 * do next. The money and the timeline both come from the durable events, so
 * they are the same numbers and the same lines the CLI shows.
 */

interface FailedStep {
  scenario_id: string
  step: string
  message: string
}

interface VerificationResult {
  kind?: string
  status?: string
  stdout?: string
  stderr?: string
  failed_steps?: FailedStep[]
  error_message?: string
}

const money = (value: string | number | undefined) => {
  const amount = Number(value ?? 0)
  return Number.isFinite(amount) ? `$${amount.toFixed(2)}` : '—'
}

export default function RunMonitor({
  run,
  events,
  onBuildAgain,
  onAmend,
  onInspect,
}: {
  run: Run
  events: RunEvent[]
  onBuildAgain: () => void
  onAmend: () => void
  onInspect: () => void
}) {
  const [usage, setUsage] = useState<RunUsage | null>(null)
  const [timeline, setTimeline] = useState<RunTimeline | null>(null)
  const [lastFailure, setLastFailure] = useState<VerificationResult | null>(null)
  const [showRaw, setShowRaw] = useState(false)

  // Refreshed whenever the event stream grows: the events are the only
  // account of a run, so a new event is exactly when these can have changed.
  useEffect(() => {
    let cancelled = false
    Promise.all([api.usage(run.id), api.timeline(run.id)])
      .then(([nextUsage, nextTimeline]) => {
        if (cancelled) return
        setUsage(nextUsage)
        setTimeline(nextTimeline)
      })
      .catch(() => {
        // The page's connection banner owns transient API errors.
      })
    return () => {
      cancelled = true
    }
  }, [run.id, events.length, run.status])

  // The last gate that failed, with the output the retry was shown.
  useEffect(() => {
    const failed = events.filter(
      (event) =>
        event.event_type === 'evidence.recorded' &&
        event.payload.status !== 'passed' &&
        typeof event.payload.result_digest === 'string',
    )
    const latest = failed[failed.length - 1]
    if (!latest || !['failed', 'canceled'].includes(run.status)) {
      setLastFailure(null)
      return
    }
    let cancelled = false
    api
      .artifact<VerificationResult>(run.id, String(latest.payload.result_digest))
      .then((artifact) => {
        if (!cancelled) setLastFailure(artifact.content ?? null)
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [events, run.id, run.status])

  const spent = usage?.used ? Number(usage.used.cost_usd) : 0
  const ceiling = usage ? Number(usage.budget.max_cost_usd) : 0
  const fraction = ceiling > 0 ? Math.min(1, spent / ceiling) : 0
  const settled = ['succeeded', 'failed', 'canceled'].includes(run.status)
  const lines = timeline?.lines ?? []
  const diagnostics = lastFailure
    ? (lastFailure.stderr?.trim() || lastFailure.stdout?.trim() || lastFailure.error_message || '')
        .split('\n')
        .slice(-24)
        .join('\n')
    : ''

  return (
    <section className="plane-panel" id="stage-monitor">
      <div className="plane-section-title">
        <div>
          <span className="plane-eyebrow">Run · {shortId(run.id)}</span>
          <h2>{settled ? 'What happened' : 'Building'}</h2>
        </div>
        <span className={`chip ${statusClass(run.status)}`}>{run.status}</span>
      </div>

      <div className="plane-meter" aria-label="Spending against the ceiling">
        <div className="plane-meter-figures">
          <div>
            <b>{money(spent)}</b>
            <span>spent</span>
          </div>
          <div>
            <b>{money(ceiling)}</b>
            <span>ceiling</span>
          </div>
          <div>
            <b>{usage?.used?.model_attempts ?? 0}<small>/{usage?.budget.max_model_attempts ?? '—'}</small></b>
            <span>model attempts</span>
          </div>
          <div>
            <b>{Math.round(((usage?.used?.input_tokens ?? 0) + (usage?.used?.output_tokens ?? 0)) / 1000)}k</b>
            <span>tokens</span>
          </div>
        </div>
        <div className="plane-meter-bar" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(fraction * 100)}>
          <span style={{ width: `${fraction * 100}%` }} />
        </div>
        <small className="plane-meter-note">
          Measured from the durable events, the way a restart recovers the budget: a started attempt
          without a settlement counts at its full reservation, so this is never optimistic.
          {usage?.recovery_error ? ` Could not recover: ${usage.recovery_error}` : ''}
        </small>
      </div>

      {['failed', 'canceled'].includes(run.status) && (
        <div className="plane-next-actions">
          <div>
            <b>{run.status === 'canceled' ? 'Stopped at a checkpoint.' : 'A gate the model cannot touch said no.'}</b>
            <p>
              A settled run keeps its result — that is what makes it evidence. Build again over the
              same approved design and every unchanged part replays from memory instead of being
              paid for twice; or change the design first.
            </p>
            {lastFailure && (
              <div className="plane-failure">
                <span className="plane-eyebrow">
                  {lastFailure.kind ?? 'gate'} · {lastFailure.status ?? 'failed'}
                </span>
                {(lastFailure.failed_steps ?? []).map((failure, index) => (
                  <div className="plane-failed-step" key={index}>
                    <b>Failed at step</b> {failure.step}
                    {failure.message && <small>{failure.message}</small>}
                  </div>
                ))}
                {diagnostics && <pre>{diagnostics}</pre>}
              </div>
            )}
          </div>
          <div className="plane-next-buttons">
            <button className="primary" onClick={onBuildAgain}>Build again →</button>
            <button onClick={onInspect}>Rebuild one component</button>
            <button onClick={onAmend}>Amend the design</button>
          </div>
        </div>
      )}

      <div className="plane-timeline" aria-label="Run timeline">
        {lines.length === 0 && <p className="muted">Waiting for the first event…</p>}
        {lines.map((line) => (
          <pre key={line.sequence} className={line.text.includes(' !! ') ? 'bad' : line.text.includes(' ok ') ? 'ok' : ''}>
            {line.text}
          </pre>
        ))}
      </div>

      <details className="plane-raw-events" open={showRaw} onToggle={(event) => setShowRaw((event.target as HTMLDetailsElement).open)}>
        <summary>{events.length} durable events, as recorded</summary>
        <div className="plane-event-feed">
          {events.map((event) => (
            <article key={event.sequence}>
              <span>{String(event.sequence).padStart(3, '0')}</span>
              <div>
                <b>{event.event_type}</b>
                <small>
                  {new Date(event.created_at).toLocaleTimeString()} · {event.task_id ? shortId(event.task_id) : 'run'}
                </small>
              </div>
              <code>{JSON.stringify(event.payload)}</code>
            </article>
          ))}
        </div>
      </details>
    </section>
  )
}
