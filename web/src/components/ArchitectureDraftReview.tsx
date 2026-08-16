import { useMemo, useState } from 'react'

import type { Architecture, ArchitectureDraft } from '../lib/v2Api'

/**
 * Review a proposal before anything records it.
 *
 * Nothing durable exists yet when this renders: no revision, no approval. The
 * human reads the change against whatever is current, then applies it, asks
 * again with a correction, or discards it. That is v1's vibe-edit loop, which
 * v2 had replaced with a binary approve/reject on an already-stored revision.
 */

interface Props {
  draft: ArchitectureDraft
  current: Architecture | null
  busy: boolean
  onApply: () => void
  onRedraft: (repair: string) => void
  onDiscard: () => void
}

type Change = { id: string; kind: 'added' | 'removed' | 'changed'; detail: string }

/** A node's signature: everything a reviewer would call a real difference. */
function signature(node: Architecture['nodes'][number]): string {
  return JSON.stringify({
    kind: node.kind,
    name: node.name,
    requirement_ids: [...(node.requirement_ids ?? [])].sort(),
    owned_paths: [...(node.owned_paths ?? [])].sort(),
    contract_id: node.contract_id,
  })
}

function diffNodes(
  current: Architecture | null,
  next: Architecture,
): Change[] {
  const before = new Map((current?.nodes ?? []).map((node) => [node.id, node]))
  const after = new Map(next.nodes.map((node) => [node.id, node]))
  const changes: Change[] = []

  for (const [id, node] of after) {
    const previous = before.get(id)
    if (!previous) {
      changes.push({
        id,
        kind: 'added',
        detail: `${node.kind} · ${(node.requirement_ids ?? []).join(', ') || 'no requirements'}`,
      })
    } else if (signature(previous) !== signature(node)) {
      const wasRequirements = [...(previous.requirement_ids ?? [])].sort().join(', ')
      const nowRequirements = [...(node.requirement_ids ?? [])].sort().join(', ')
      changes.push({
        id,
        kind: 'changed',
        detail:
          wasRequirements === nowRequirements
            ? `${previous.name} → ${node.name}`
            : `requirements ${wasRequirements || 'none'} → ${nowRequirements || 'none'}`,
      })
    }
  }
  for (const [id, node] of before) {
    if (!after.has(id)) {
      changes.push({ id, kind: 'removed', detail: node.name })
    }
  }
  return changes.sort((left, right) => left.id.localeCompare(right.id))
}

const MARK = { added: '+', removed: '−', changed: '~' } as const

export default function ArchitectureDraftReview({
  draft,
  current,
  busy,
  onApply,
  onRedraft,
  onDiscard,
}: Props) {
  const [repair, setRepair] = useState('')
  const changes = useMemo(() => diffNodes(current, draft.architecture), [current, draft])

  return (
    <section className="v2-panel v2-draft">
      <div className="v2-section-title">
        <div>
          <span className="v2-eyebrow">
            Proposed by {draft.source === 'model' ? 'the architect' : 'the deterministic planner'}
          </span>
          <h2>Review before anything is recorded</h2>
        </div>
        <span className="chip">{changes.length || 'no'} changes</span>
      </div>

      {draft.rationale && <p className="muted">{draft.rationale}</p>}

      <div className="v2-draft-diff">
        {changes.length === 0 && (
          <p className="muted">
            This proposal matches the current architecture. Applying it would
            record an identical revision.
          </p>
        )}
        {changes.map((change) => (
          <div key={`${change.kind}:${change.id}`} className={`d-${change.kind}`}>
            <b>
              {MARK[change.kind]} {change.id}
            </b>
            <small>{change.detail}</small>
          </div>
        ))}
      </div>

      {draft.decisions.length > 0 && (
        <div className="v2-decision-grid">
          <div>
            <h4>Decisions</h4>
            <ul>
              {draft.decisions.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </div>
          {draft.risks.length > 0 && (
            <div>
              <h4>Rejected attempts</h4>
              <ul>
                {draft.risks.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}

      <label className="v2-repair">
        <span>Ask for a different design</span>
        <textarea
          rows={2}
          value={repair}
          placeholder="The checklist logic belongs in its own component, not in the web layer."
          onChange={(event) => setRepair(event.target.value)}
        />
      </label>

      <div className="v2-draft-actions">
        <button className="primary" disabled={busy} onClick={onApply}>
          Apply as a new revision
        </button>
        <button
          disabled={busy || !repair.trim()}
          onClick={() => onRedraft(repair.trim())}
          title="Sends your correction to the architect and proposes again"
        >
          Propose again with this note
        </button>
        <button disabled={busy} onClick={onDiscard}>
          Discard
        </button>
      </div>
      <p className="muted v2-draft-note">
        Applying records a new immutable revision that needs its own approval.
        Nothing has been stored yet.
      </p>
    </section>
  )
}
