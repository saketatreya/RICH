import { useStore, buildReady } from '../store'
import { blockingCards, flatten, unplannedNodes } from '../lib/tree'

export default function Topbar({ slot }: { slot: 'brand' | 'status' }) {
  const config = useStore((s) => s.config)
  const tree = useStore((s) => s.tree)
  const cards = useStore((s) => s.cards)
  const jobRunning = useStore((s) => s.job.status === 'running')
  const setBriefOpen = useStore((s) => s.setBriefOpen)
  const reset = useStore((s) => s.reset)
  const build = useStore((s) => s.build)
  const mutate = useStore((s) => s.mutate)

  if (slot === 'brand') {
    return (
      <div className="brand-lock glass">
        <div className="brand-mark" />
        <div className="brand-name">RI<b>CH</b></div>
        <span className="chip" style={{ marginLeft: 2 }}>
          {config?.backend || '…'} {config ? (config.backend_available ? '·on' : '·off') : ''}
        </span>
      </div>
    )
  }

  const ready = buildReady(tree, cards)
  const unplanned = unplannedNodes(tree).length
  const errors = blockingCards(cards).length

  let chip = { text: 'no project', cls: '' }
  if (tree) {
    if (ready) chip = { text: 'ready to build', cls: 'ok' }
    else if (errors) chip = { text: `${errors} issue${errors === 1 ? '' : 's'}`, cls: 'bad' }
    else chip = { text: `${unplanned} to plan`, cls: 'warn' }
  }

  const doBuild = () => {
    if (!tree || !ready) return
    const all = flatten(tree)
    const verified = all.filter((n) => n.status === 'verified').length
    const toBuild = all.length - verified
    if (confirm(`Build ${all.length} modules · ${verified} verified (reuse) · ${toBuild} to build.\nThis spends LLM calls. Continue?`)) build()
  }
  const tidy = () => mutate((t) => flatten(t).forEach((n) => { delete n.x; delete n.y }))

  return (
    <div className="statuscluster glass">
      {tree && <span className={'chip ' + chip.cls}>{ready ? '✓ ' : ''}{chip.text}</span>}
      <button className="ghost sm" onClick={() => setBriefOpen(true)}>Brief</button>
      <button className="ghost sm" disabled={!tree} onClick={tidy} title="Re-layout">Tidy</button>
      <button className="ghost sm" disabled={jobRunning} onClick={() => { if (!tree || confirm('Clear the current tree?')) reset() }}>Reset</button>
      <button className="go sm" disabled={!ready || jobRunning} onClick={doBuild}>Build ▶</button>
    </div>
  )
}
