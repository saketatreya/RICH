import { useEffect, useRef, useState } from 'react'

import { api, type ChangePlan, type ChangeRevisions } from '../lib/api'

/**
 * What an amendment costs, before you commit to it.
 *
 * This is the view that makes "modular" mean something to the person doing the
 * work. An architect changing one requirement wants to know which components
 * that touches and which are untouched — and the answer is derived from the
 * contracts, not guessed.
 *
 * Planning is a read: it compares two approved revisions and calls no model,
 * so the cost appears as soon as there are two designs to compare rather than
 * waiting to be asked for. The M4 drive found the alternative -- a customer
 * who had approved a redraft was offered Build beside a panel that named a
 * cost it had not computed, and would have committed the money without ever
 * being shown what it bought.
 *
 * Applying withdraws permission to replay the stale components' last answers,
 * and nothing else: it cannot touch evidence, because evidence is never
 * replayed in the first place.
 */

interface Props {
  projectId: string
  revisions: ChangeRevisions | null
  /** Mark the stale components, then build over the newest approved design. */
  onApplyAndBuild?: () => void
  busy?: boolean
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
    <div className={`plane-change-bucket ${tone}`}>
      <b>{title}</b>
      <div className="plane-change-nodes">
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

export default function ChangeCost({ projectId, revisions, onApplyAndBuild, busy: outerBusy }: Props) {
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

  // One automatic computation per approved pair. `planned` remembers which
  // pair was asked about, so approving a further redraft recomputes and a
  // re-render does not.
  const planned = useRef<string | null>(null)
  const pair = revisions
    ? [
        revisions.fromSpec,
        revisions.toSpec,
        revisions.fromArchitecture,
        revisions.toArchitecture,
      ].join('|')
    : null

  useEffect(() => {
    if (!revisions || !pair || planned.current === pair) return
    planned.current = pair
    setPlan(null)
    void run('plan', () => api.planChange(projectId, revisions))
    // `run` is stable for the life of the component and `projectId` cannot
    // change without the pair changing with it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pair])

  const change = plan?.change
  const applied = plan?.forgotten !== undefined

  return (
    <section className="plane-panel" id="stage-change">
      <div className="plane-section-title">
        <div>
          <span className="plane-eyebrow">Change</span>
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
        <p className="plane-note">
          Amend the specification, approve it, and approve a redrafted architecture:
          the cost is computed between the last two approved designs.
        </p>
      )}

      {revisions && (
        <div className="plane-submit-actions">
          <button
            className="primary"
            disabled={!!busy}
            onClick={() => run('plan', () => api.planChange(projectId, revisions))}
          >
            {busy === 'plan' ? 'Computing…' : 'Compute again'}
          </button>
          {change && change.stale.length > 0 && !applied && (
            <button
              className="plane-secondary"
              disabled={!!busy}
              onClick={() =>
                run('apply', () => api.applyChange(projectId, revisions))
              }
            >
              {busy === 'apply' ? 'Marking…' : 'Mark these stale'}
            </button>
          )}
          {change && onApplyAndBuild && (
            <button
              className="go"
              disabled={!!busy || !!outerBusy}
              onClick={async () => {
                if (!applied && change.stale.length > 0) {
                  await run('apply', () => api.applyChange(projectId, revisions))
                }
                onApplyAndBuild()
              }}
              title="Forget the stale components' remembered answers, then build over the newest approved design; every gate runs again regardless"
            >
              Apply and build →
            </button>
          )}
        </div>
      )}

      {error && <p className="plane-note-warn">{error}</p>}

      {change && (
        <>
          {(change.requirements.modified.length > 0 ||
            change.requirements.added.length > 0 ||
            change.requirements.removed.length > 0) && (
            <div className="plane-change-requirements">
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

          <div className="plane-change-grid">
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

          <ul className="plane-change-notes">
            {change.notes.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>

          {applied && (
            <p className="plane-note">
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
