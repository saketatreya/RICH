import { useState } from 'react'
import { useStore } from '../store'
import { api } from '../lib/api'
import type { TreeNode } from '../lib/types'

interface Brief {
  goal: string
  root_id: string
  external_services: string
  inputs: string
  outputs: string
  state: string
  shape: string
  strict: boolean
  noLive: boolean
}

const EMPTY: Brief = {
  goal: '', root_id: '', external_services: '', inputs: '', outputs: '',
  state: 'stateless', shape: 'simple', strict: true, noLive: true,
}

function proposalRows(n: TreeNode, depth = 0, rows: any[] = []) {
  rows.push({ id: n.id, depth, kind: n.kind || (n.stateful ? 'stateful' : 'pure'), description: n.description || '', childCount: (n.children || []).length })
  ;(n.children || []).forEach((c) => proposalRows(c, depth + 1, rows))
  return rows
}

export default function BriefModal() {
  const open = useStore((s) => s.briefOpen)
  const tree = useStore((s) => s.tree)
  const setBriefOpen = useStore((s) => s.setBriefOpen)
  const createRoot = useStore((s) => s.createRoot)
  const applyProposalTree = useStore((s) => s.applyProposalTree)

  const [b, setB] = useState<Brief>(EMPTY)
  const [busy, setBusy] = useState(false)
  const [suggestion, setSuggestion] = useState<any>(null)
  const [proposal, setProposal] = useState<any>(null)

  if (!open) return null
  const set = (patch: Partial<Brief>) => setB({ ...b, ...patch })

  const payload = () => ({
    goal: b.goal.trim(),
    root_id: b.root_id.trim(),
    external_services: b.external_services.trim(),
    inputs: b.inputs.trim(),
    outputs: b.outputs.trim(),
    state: b.state,
    shape: b.shape,
    rigor: [b.strict ? 'strict fakeable unit tests' : '', b.noLive ? 'no live unit tests' : ''].filter(Boolean).join(', '),
    flags: [b.strict ? 'strict_tests' : null, b.noLive ? 'no_live_unit_io' : null].filter(Boolean),
  })

  const suggest = async () => {
    setBusy(true)
    try {
      const res = await api.suggestBrief(payload(), tree)
      setSuggestion(res)
      const s = res.suggestion || res.preview || {}
      set({
        goal: b.goal || s.goal || s.description || '',
        root_id: b.root_id || s.root_id || '',
        external_services: b.external_services || s.external_services || '',
        inputs: b.inputs || s.inputs || '',
        outputs: b.outputs || s.outputs || '',
        state: s.state || b.state,
        shape: s.shape || b.shape,
      })
    } finally {
      setBusy(false)
    }
  }

  const draft = async () => {
    setBusy(true)
    try {
      const res = await api.propose(payload(), tree)
      setProposal(res)
    } finally {
      setBusy(false)
    }
  }

  const create = () => {
    const desc = b.goal.trim()
    if (!desc) {
      alert('Describe the goal first.')
      return
    }
    let id = b.root_id.trim() || desc.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '').slice(0, 32) || 'root'
    createRoot(id, desc)
  }

  const rows = proposal ? proposalRows(proposal.preview_tree || {}) : []

  return (
    <div className="modal">
      <div className="card glass">
        <div className="proposal-head">
          <div><h2>Architecture brief</h2><p className="lead">Describe the software and the system qualities RICH should preserve.</p></div>
          <span className="sp" />
          <button className="sm ghost" disabled={!tree} onClick={() => setBriefOpen(false)}>Close</button>
        </div>

        <div className="brief-grid">
          <div className="field span2"><label>Goal</label>
            <textarea value={b.goal} onChange={(e) => set({ goal: e.target.value })} placeholder="Fetch current weather for a city and print a short report." /></div>
          <div className="field"><label>Root module id (optional)</label>
            <input className="mono" value={b.root_id} onChange={(e) => set({ root_id: e.target.value })} placeholder="weather_reporter" /></div>
          <div className="field"><label>External services (optional)</label>
            <input value={b.external_services} onChange={(e) => set({ external_services: e.target.value })} placeholder="Open-Meteo, Stripe, filesystem…" /></div>
          <div className="field"><label>Input example (optional)</label>
            <input value={b.inputs} onChange={(e) => set({ inputs: e.target.value })} placeholder="city: London" /></div>
          <div className="field"><label>Output example (optional)</label>
            <input value={b.outputs} onChange={(e) => set({ outputs: e.target.value })} placeholder="Short weather report string" /></div>
          <div className="field"><label>State (optional)</label>
            <select value={b.state} onChange={(e) => set({ state: e.target.value })}>
              <option value="stateless">stateless</option><option value="cached">cached</option><option value="persistent">persistent</option></select></div>
          <div className="field"><label>Architecture shape (optional)</label>
            <select value={b.shape} onChange={(e) => set({ shape: e.target.value })}>
              <option value="simple">simple</option><option value="modular">modular</option><option value="production">production</option></select></div>
          <label className="inline-check"><input type="checkbox" checked={b.strict} onChange={(e) => set({ strict: e.target.checked })} /> strict fakeable unit tests</label>
          <label className="inline-check"><input type="checkbox" checked={b.noLive} onChange={(e) => set({ noLive: e.target.checked })} /> no live provider calls in unit tests</label>
        </div>

        <div className="row" style={{ marginTop: 14 }}>
          <button className="sm ghost" disabled={busy} onClick={suggest}>Suggest root details</button>
          <button className="primary" disabled={busy} onClick={draft}>Draft architecture proposal</button>
          <button className="ghost" onClick={create}>Create root</button>
        </div>

        {suggestion && (
          <div className="proposal-box">
            <div className="proposal-head"><div><h3>Root draft</h3><p>{suggestion.source || 'deterministic'} suggestion.</p></div></div>
            <ul className="proposal-list">
              {Object.entries((suggestion.preview || suggestion.suggestion || {}) as Record<string, any>).map(([k, v]) => (
                <li key={k}><b>{k}</b> <span className="muted">· {String(v)}</span></li>
              ))}
            </ul>
          </div>
        )}

        {proposal && (
          <div className="proposal-box">
            <div className="proposal-head">
              <div><h3>{proposal.transaction?.label || 'Architecture proposal'}</h3>
                <p>{proposal.transaction?.summary || 'Preview before replacing the canvas.'}</p></div>
              <span className="sp" />
              <button className="primary sm" onClick={() => applyProposalTree(proposal.preview_tree, proposal.transaction)}>Apply proposal</button>
            </div>
            <div className="muted" style={{ fontSize: 12, marginBottom: 8 }}>
              {(proposal.preview_tree?.id) || 'root'} · {rows.length} modules · {proposal.transaction?.source || 'deterministic'}
            </div>
            <ul className="proposal-list">
              {rows.map((r) => (
                <li key={r.id} style={{ paddingLeft: 12 * r.depth }}>
                  <b>{r.id}</b> <span className="muted">· {r.kind}{r.childCount ? ` · ${r.childCount} child${r.childCount === 1 ? '' : 'ren'}` : ''}</span>
                  <div className="muted" style={{ marginTop: 2 }}>{r.description}</div>
                </li>
              ))}
            </ul>
            {(proposal.checklist || []).length > 0 && (
              <>
                <div className="proposal-head" style={{ marginTop: 14 }}><div><h3>Checklist</h3></div></div>
                {(proposal.checklist || []).map((c: any, i: number) => (
                  <div key={i} className={`check-row ${c.ok ? 'ok' : 'bad'}`}>
                    <div style={{ flex: 1 }}><b>{c.ok ? '✓' : '!'} {c.title}</b><span>{c.message}</span></div>
                  </div>
                ))}
              </>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
