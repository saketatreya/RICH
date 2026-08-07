import { useState } from 'react'
import { useStore } from '../store'
import { findNode } from '../lib/tree'
import type { Kind, Op, TreeNode } from '../lib/types'

function kvText(o: Record<string, any>) {
  return Object.entries(o || {})
    .map(([k, v]) => `${k}: ${typeof v === 'object' ? JSON.stringify(v) : v}`)
    .join('\n')
}
function parseKV(t: string): Record<string, any> {
  const o: Record<string, any> = {}
  ;(t || '').split('\n').forEach((line) => {
    const l = line.trim()
    if (!l) return
    const i = l.indexOf(':')
    if (i < 0) {
      o[l] = 'any'
      return
    }
    const k = l.slice(0, i).trim()
    let v: any = l.slice(i + 1).trim()
    try {
      if (/^[[{]/.test(v)) v = JSON.parse(v)
    } catch {}
    o[k] = v
  })
  return o
}

const KINDS: Kind[] = ['pure', 'stateful', 'adapter', 'ui']

export default function EditForm({ node }: { node: TreeNode }) {
  const mutate = useStore((s) => s.mutate)
  const renameNode = useStore((s) => s.renameNode)
  const setEditMode = useStore((s) => s.setEditMode)

  const ext = { provider: '', mock_policy: 'unit_tests_mock_provider', live_smoke: false, ...(node.external || {}) }
  const [id, setId] = useState(node.id)
  const [desc, setDesc] = useState(node.description || '')
  const [kind, setKind] = useState<Kind>((node.kind || 'pure') as Kind)
  const [provider, setProvider] = useState(ext.provider || '')
  const [liveSmoke, setLiveSmoke] = useState(!!ext.live_smoke)
  const [ops, setOps] = useState<Op[]>(JSON.parse(JSON.stringify(node.operations || [])))
  const [opText, setOpText] = useState<Record<number, { inputs: string; outputs: string; errors: string }>>(() => {
    const m: any = {}
    ;(node.operations || []).forEach((op, i) => {
      m[i] = { inputs: kvText(op.inputs), outputs: kvText(op.outputs), errors: (op.errors || []).join(', ') }
    })
    return m
  })

  const save = () => {
    const collected: Op[] = ops.map((op, i) => ({
      name: op.name.trim(),
      inputs: parseKV(opText[i]?.inputs || ''),
      outputs: parseKV(opText[i]?.outputs || ''),
      errors: (opText[i]?.errors || '').split(',').map((s) => s.trim()).filter(Boolean),
    }))
    mutate((t) => {
      const n = findNode(t, node.id)!
      n.description = desc
      n.kind = kind
      n.stateful = kind === 'stateful'
      n.operations = collected
      n.external = { provider: provider.trim(), mock_policy: 'unit_tests_mock_provider', live_smoke: liveSmoke }
    }, { pushUndo: true })
    if (id.trim() && id.trim() !== node.id) renameNode(node.id, id.trim())
    setEditMode(false)
  }

  return (
    <>
      <div className="sec">
        <div className="field"><label>module id</label><input className="mono" value={id} onChange={(e) => setId(e.target.value)} /></div>
        <div className="field"><label>purpose (plain language)</label><textarea value={desc} onChange={(e) => setDesc(e.target.value)} /></div>
        <div className="field"><label>module kind</label>
          <select value={kind} onChange={(e) => setKind(e.target.value as Kind)}>{KINDS.map((k) => <option key={k} value={k}>{k}</option>)}</select>
        </div>
        <div className="field"><label>adapter provider</label><input value={provider} onChange={(e) => setProvider(e.target.value)} placeholder="Open-Meteo, Stripe, filesystem…" /></div>
        <label className="inline-check" style={{ marginBottom: 8 }}>
          <input type="checkbox" checked={liveSmoke} onChange={(e) => setLiveSmoke(e.target.checked)} /> allow opt-in live smoke test for this adapter
        </label>
      </div>

      <div className="sec">
        <h4>Operations</h4>
        {ops.map((op, i) => (
          <div key={i} className="op">
            <div className="field"><label>name</label>
              <input className="mono" value={op.name} onChange={(e) => setOps(ops.map((o, j) => (j === i ? { ...o, name: e.target.value } : o)))} /></div>
            <div className="field"><label>inputs (one <code>key: type</code> per line)</label>
              <textarea value={opText[i]?.inputs || ''} onChange={(e) => setOpText({ ...opText, [i]: { ...opText[i], inputs: e.target.value } })} /></div>
            <div className="field"><label>outputs</label>
              <textarea value={opText[i]?.outputs || ''} onChange={(e) => setOpText({ ...opText, [i]: { ...opText[i], outputs: e.target.value } })} /></div>
            <div className="field"><label>errors (comma-separated)</label>
              <input value={opText[i]?.errors || ''} onChange={(e) => setOpText({ ...opText, [i]: { ...opText[i], errors: e.target.value } })} /></div>
            <button className="sm danger" onClick={() => { setOps(ops.filter((_, j) => j !== i)) }}>remove operation</button>
          </div>
        ))}
        <button className="sm" style={{ marginTop: 4 }} onClick={() => {
          setOps([...ops, { name: '', inputs: {}, outputs: {}, errors: [] }])
          setOpText({ ...opText, [ops.length]: { inputs: '', outputs: '', errors: '' } })
        }}>+ operation</button>
      </div>

      <div className="sec">
        <div className="row">
          <button className="primary sm" onClick={save}>Save</button>
          <button className="sm ghost" onClick={() => setEditMode(false)}>Cancel</button>
        </div>
      </div>
    </>
  )
}
