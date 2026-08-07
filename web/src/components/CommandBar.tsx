import { useEffect, useState } from 'react'
import { useStore } from '../store'
import { flatten } from '../lib/tree'
import type { TreeNode } from '../lib/types'

const CHIPS = [
  { label: 'Externalize IO', prompt: 'make this testable by isolating external IO behind adapters' },
  { label: 'Add cache', prompt: 'add a cache boundary for the selected module' },
  { label: 'Mark UI', prompt: 'mark this as the user interface boundary' },
  { label: 'Make leaf', prompt: 'mark this module as a leaf' },
]

function sig(n: TreeNode) {
  return JSON.stringify({ d: n.description, k: n.kind, l: n.is_leaf, o: n.operations, dep: n.dependencies, e: n.edges, x: n.external })
}
function diffTrees(before: TreeNode, after: TreeNode) {
  const b: Record<string, TreeNode> = {}, a: Record<string, TreeNode> = {}
  flatten(before).forEach((n) => (b[n.id] = n))
  flatten(after).forEach((n) => (a[n.id] = n))
  return {
    added: Object.keys(a).filter((id) => !b[id]),
    removed: Object.keys(b).filter((id) => !a[id]),
    changed: Object.keys(a).filter((id) => b[id] && sig(b[id]) !== sig(a[id])),
  }
}

export default function CommandBar() {
  const tree = useStore((s) => s.tree)
  const selectedId = useStore((s) => s.selectedId)
  const jobRunning = useStore((s) => s.job.status === 'running')
  const vibeDraft = useStore((s) => s.vibeDraft)
  const pendingVibe = useStore((s) => s.pendingVibe)
  const previewVibe = useStore((s) => s.previewVibe)
  const applyVibe = useStore((s) => s.applyVibe)
  const discardVibe = useStore((s) => s.discardVibe)

  const [text, setText] = useState('')
  const [scope, setScope] = useState<string | null>(null)

  useEffect(() => {
    if (vibeDraft) { setText(vibeDraft.prompt); setScope(vibeDraft.nodeId) }
  }, [vibeDraft])

  const run = (prompt?: string) => {
    const p = (prompt ?? text).trim()
    if (!p || !tree) return
    previewVibe(p, prompt ? selectedId : scope ?? selectedId)
    if (!prompt) { setText(''); setScope(null) }
  }

  const diff = pendingVibe ? diffTrees(pendingVibe.before, pendingVibe.tree) : null

  return (
    <div className="cmdwrap">
      <div className="cmdbar glass">
        <span className="spark">✦</span>
        <input
          placeholder={tree ? (selectedId ? `Vibe ${selectedId}: add caching, split IO out, simplify…` : 'Vibe the architecture…') : 'Open a brief to begin'}
          value={text}
          disabled={!tree}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') run() }}
        />
        <span className="kbd">↵</span>
        <button className="primary sm" disabled={!tree || jobRunning || !text.trim()} onClick={() => run()}>Vibe</button>
      </div>

      {tree && !pendingVibe && (
        <div className="cmdchips">
          {CHIPS.map((c) => (
            <span key={c.label} className="chip" onClick={() => !jobRunning && run(c.prompt)}>{c.label}</span>
          ))}
        </div>
      )}

      {pendingVibe && diff && (
        <div className="diff-pop glass">
          <div className="dh">
            Proposed change {pendingVibe.transaction?.source ? <span className="faint mono" style={{ fontSize: 11 }}>· {pendingVibe.transaction.source}</span> : null}
          </div>
          <div className="muted" style={{ fontSize: 12 }}>{pendingVibe.transaction?.summary || pendingVibe.transaction?.label || 'review the diff'}</div>
          <div className="diff">
            {diff.added.map((id) => <div key={id} className="add">+ {id}</div>)}
            {diff.removed.map((id) => <div key={id} className="rm">− {id}</div>)}
            {diff.changed.map((id) => <div key={id} className="chg">~ {id}</div>)}
            {!diff.added.length && !diff.removed.length && !diff.changed.length && <div className="faint">no structural change</div>}
          </div>
          <div className="row">
            <button className="primary sm" onClick={applyVibe}>Apply</button>
            <button className="ghost sm" onClick={discardVibe}>Discard</button>
          </div>
        </div>
      )}
    </div>
  )
}
