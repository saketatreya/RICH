/** One human decision, with the thing it authorizes stated plainly.
 *
 * Approvals bind an exact revision, so the copy here has to say what is
 * being approved rather than just ask for a click. */

import type { Approval } from '../lib/api'
import { shortId, statusClass } from '../lib/format'

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
    <section className="v2-gate">
      <div className="v2-gate-icon">{approval.status === 'approved' ? '✓' : '◆'}</div>
      <div className="v2-gate-main">
        <div className="v2-section-title">
          <div>
            <span className="v2-eyebrow">Human authority</span>
            <h3>{title}</h3>
          </div>
          <span className={`chip ${statusClass(approval.status)}`}>{approval.status}</span>
        </div>
        <p>{description}</p>
        <div className="v2-idline" title={approval.id}>
          <span>Approval</span>
          <code>{shortId(approval.id)}</code>
        </div>
        {approval.decision && (
          <div className="v2-decision">
            Decided by <b>{String(approval.decision.actor || 'unknown')}</b>
            {approval.decision.reason ? ` · ${String(approval.decision.reason)}` : ''}
          </div>
        )}
        {approval.status === 'requested' && (
          <div className="v2-actions">
            <button
              className="primary"
              disabled={busy || !actor.trim()}
              onClick={() => onDecision(true)}
            >
              Approve gate
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
