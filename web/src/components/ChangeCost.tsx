import { useState } from 'react'

import { api, type ChangePlan, type ChangeRevisions } from '../lib/api'

/**
 * What an amendment costs, before you commit to it.
 *
 * This is the view that makes "modular" mean something to the person doing the
 * work. An architect changing one requirement wants to know which components
 * that touches and which are untouched — and the answer is derived from the
 * contracts, not guessed.
 *
 * Planning is a read. Applying withdraws permission to replay the stale
 * components' last answers, and nothing else: it cannot touch evidence,
 * because evidence is never replayed in the first place.
 */

interface Props {
  projectId: string
  revisions: ChangeRevisions | null
}

function Bucket({
  title,
  nodes,
  tone,
  reason,
}: {
  title: string
  nodes: string[]
  tone: 'stale' | 'reuse'
  reason: string
}) {
  if (nodes.length === 0) return null
  return (
    <div className={`v2-change-bucket ${tone}`}>
      <b>{title}</b>
      <div className="v2-change-nodes">
        {nodes.map((node) => (
          <span className="chip" key={node}>
            {node}
          </span>
        ))}
      </div>
      <small>{reason}</small>
    </div>
  )
}

export default function ChangeCost({ projectId, revisions }: Props) {
  const [plan, setPlan] = useState<ChangePlan | null>(null)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState<string | null>(null)

  const run = async (label: string, work: () => Promise<ChangePlan>) => {
    setBusy(label)
    setError(null)
    try {
      setPlan(await work())
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setBusy('')
    }
  }

  const change = plan?.change
  const applied = plan?.forgotten !== undefined

  return (
    <section className="v2-panel" id="stage-change">
      <div className="v2-section-title">
        <div>
          <span className="v2-eyebrow">Change</span>
          <h2>What this amendment costs</h2>
        </div>
        {change && (
          <span className={`chip ${change.stale.length ? 'warn' : 'ok'}`}>
            {change.stale.length} of {change.stale.length + change.reusable.length}{' '}
            stale
          </span>
        )}
      </div>

      <p className="muted">
        The blast radius is derived from the contracts, not guessed. A component
        whose implementation changed cannot affect its consumers — none of them
        was ever shown it. A component whose contract changed invalidates every
        consumer, because the contract is exactly what they were shown.
      </p>

      {!revisions && (
        <p className="v2-note-warn">
          Two approved revisions are needed to compare. Amend the specification
          and approve it, then come back.
        </p>
      )}

      {revisions && (
        <div className="v2-submit-actions">
          <button
            className="primary"
            disabled={!!busy}
            onClick={() => run('plan', () => api.planChange(projectId, revisions))}
          >
            {busy === 'plan' ? 'Computing…' : 'Compute the cost'}
          </button>
          {change && change.stale.length > 0 && !applied && (
            <button
              className="v2-secondary"
              disabled={!!busy}
              onClick={() =>
                run('apply', () => api.applyChange(projectId, revisions))
              }
            >
              {busy === 'apply' ? 'Marking…' : 'Mark these stale'}
            </button>
          )}
        </div>
      )}

      {error && <p className="v2-note-warn">{error}</p>}

      {change && (
        <>
          {(change.requirements.modified.length > 0 ||
            change.requirements.added.length > 0 ||
            change.requirements.removed.length > 0) && (
            <div className="v2-change-requirements">
              {(
                [
                  ['modified', change.requirements.modified],
                  ['added', change.requirements.added],
                  ['removed', change.requirements.removed],
                ] as const
              ).map(([label, ids]) =>
                ids.length ? (
                  <span key={label}>
                    <b>{label}</b> {ids.join(', ')}
                  </span>
                ) : null,
              )}
            </div>
          )}

          <div className="v2-change-grid">
            <Bucket
              title="Rebuilt"
              nodes={change.directly_stale}
              tone="stale"
              reason="Answers for a requirement that changed, or is being asked to do a different job."
            />
            <Bucket
              title="Contract changed"
              nodes={change.contract_changed}
              tone="stale"
              reason="Its promise is different, so what its consumers were told is different."
            />
            <Bucket
              title="Downstream"
              nodes={change.consumers_stale}
              tone="stale"
              reason="Consumes a contract that changed."
            />
            <Bucket
              title="Untouched"
              nodes={change.reusable}
              tone="reuse"
              reason="Replays its last answer. It is still verified from scratch."
            />
          </div>

          <ul className="v2-change-notes">
            {change.notes.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>

          {applied && (
            <p className="v2-note">
              Marked stale:{' '}
              {Object.entries(plan.forgotten ?? {})
                .map(([node, count]) => `${node} (${count})`)
                .join(', ') || 'nothing to forget'}
              . The next run rewrites those and replays the rest.
            </p>
          )}
        </>
      )}
    </section>
  )
}
