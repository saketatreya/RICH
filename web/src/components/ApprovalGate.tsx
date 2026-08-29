/** One human decision, with the thing it authorizes stated plainly.
 *
 * Approvals bind an exact revision, so the copy here has to say what is
 * being approved rather than just ask for a click. */

import type { Approval } from '../lib/api'
import { statusClass } from '../lib/format'

export default function ApprovalGate({
  title,
  description,
  approval,
  actor,
  busy,
  onDecision,
}: {
  title: string
  description: string
  approval: Approval
  actor: string
  busy: boolean
  onDecision: (approved: boolean) => void
}) {
  return (
    <section className="plane-gate">
      <div className="plane-gate-icon">{approval.status === 'approved' ? '✓' : '◆'}</div>
      <div className="plane-gate-main">
        <div className="plane-section-title">
          <div>
            <span className="plane-eyebrow">Human authority</span>
            <h3>{title}</h3>
          </div>
          <span className={`chip ${statusClass(approval.status)}`} title={approval.id}>
            {approval.status}
          </span>
        </div>
        <p>{description}</p>
        {approval.decision && (
          <div className="plane-decision">
            Decided by <b>{String(approval.decision.actor || 'unknown')}</b>
            {approval.decision.reason ? ` · ${String(approval.decision.reason)}` : ''}
          </div>
        )}
        {approval.status === 'requested' && (
          <div className="plane-actions">
            <button
              className="primary"
              disabled={busy || !actor.trim()}
              onClick={() => onDecision(true)}
            >
              Approve
            </button>
            <button
              className="danger"
              disabled={busy || !actor.trim()}
              onClick={() => onDecision(false)}
            >
              Reject
            </button>
          </div>
        )}
      </div>
    </section>
  )
}
