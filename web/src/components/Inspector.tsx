import { useEffect, useState } from 'react'
import { useStore } from '../store'
import { api } from '../lib/api'
import { findNode, flatten, nodeKind, normDep, parentOf } from '../lib/tree'
import type { Artifacts, Op, TreeNode } from '../lib/types'
import EditForm from './EditForm'
import CodeBlock from './CodeBlock'

function typeStr(v: any): string {
  if (v === null) return 'null'
  if (Array.isArray(v)) return '[' + v.map(typeStr).join(', ') + ']'
  if (typeof v === 'object') return '{ ' + Object.entries(v).map(([k, t]) => `${k}: ${typeStr(t)}`).join(', ') + ' }'
  return String(v)
}
function shapeNL(obj: Record<string, any>) {
  const ks = Object.keys(obj || {})
  return ks.length ? ks.map((k) => `${k}: ${typeStr(obj[k])}`).join(', ') : '—'
}

function OpView({ op }: { op: Op }) {
  const errs = (op.errors || []).join(', ')
  return (
    <div className="op">
      <div className="opname">{op.name || '(unnamed)'}</div>
      <div className="opio"><span className="l">in</span><span className="v">{shapeNL(op.inputs)}</span></div>
      <div className="opio"><span className="l">out</span><span className="v">{shapeNL(op.outputs)}</span></div>
      {errs && <div className="opio"><span className="l err">raises</span><span className="v">{errs}</span></div>}
    </div>
  )
}

function CardsSection() {
  const cards = useStore((s) => s.cards)
  const setVibeDraft = useStore((s) => s.setVibeDraft)
  const select = useStore((s) => s.select)
  if (!cards.length) return null
  return (
    <div className="cardstack">
      {cards.map((c, i) => (
        <div key={i} className={`vcard ${c.severity || 'info'}`}>
          <div className="ct">{c.title || 'Note'}{c.node_id ? <> · <code>{c.node_id}</code></> : null}</div>
          <div className="cm">{c.message || ''}</div>
          {c.action === 'mark_adapter' && c.node_id && (
            <div className="ca">
              <button className="tiny" onClick={() => { select(c.node_id!); setVibeDraft({ prompt: `mark ${c.node_id} as an external adapter`, nodeId: c.node_id! }) }}>Mark adapter</button>
            </div>
          )}
        </div>
      ))}
    </div>
  )
}

function ArtifactsView({ node }: { node: TreeNode }) {
  const [art, setArt] = useState<Artifacts | null>(null)
  const build = useStore((s) => s.build)
  const setVibeDraft = useStore((s) => s.setVibeDraft)
  const jobRunning = useStore((s) => s.job.status === 'running')
  const status = node.status

  useEffect(() => {
    let live = true
    if (status === 'verified' || status === 'failed') api.node(node.id).then((a) => live && setArt(a))
    else setArt(null)
    return () => { live = false }
  }, [node.id, status])

  if (!art || !art.built) return null
  const report = art.report
  const failed = art.status === 'failed'

  const vibeFix = () => {
    const hint = report?.failures?.length
      ? `Fix ${node.id}: its tests fail — ${report.failures.slice(0, 4).join(' | ')}. Adjust the contract/behavior so the implementation can satisfy the tests.`
      : `Fix ${node.id}: ${art.reason || 'it failed to build'}.`
    setVibeDraft({ prompt: hint, nodeId: node.id })
  }

  return (
    <>
      {failed && (
        <div className="sec">
          <div className="fail-banner">
            <b>✗ build failed</b>
            {art.reason && <div className="muted" style={{ marginTop: 4, fontSize: 12 }}>{art.reason}</div>}
          </div>
          <div className="rowbtns">
            <button className="primary sm" disabled={jobRunning} onClick={() => build(node.id)}>↻ Rebuild this module</button>
            <button className="sm" disabled={jobRunning} onClick={vibeFix}>✦ Vibe a fix</button>
          </div>
        </div>
      )}

      {report && (
        <div className="sec">
          <h4>Tests</h4>
          <div className="test-summary">
            <span className={report.passed ? 'ok' : 'bad'}>{report.passed ? '✓ passed' : '✗ failed'}</span>
            <span className="faint">· {report.attempts} attempt{report.attempts === 1 ? '' : 's'}</span>
          </div>
          {report.failures?.map((f, i) => <div key={i} className="testline fail">{f}</div>)}
          {Object.entries(art.tests).map(([fname, body]) => (
            <details key={fname} className="artifact"><summary>{fname}</summary><CodeBlock code={body} /></details>
          ))}
        </div>
      )}

      <div className="sec">
        <h4>Generated code</h4>
        {Object.entries(art.src).map(([fname, body]) => (
          <details key={fname} className="artifact" open={Object.keys(art.src).length === 1}><summary>{fname}</summary><CodeBlock code={body} /></details>
        ))}
        {!failed && (
          <div className="rowbtns" style={{ marginTop: 9 }}>
            <button className="sm" disabled={jobRunning} onClick={() => build(node.id)}>↻ Rebuild this module</button>
          </div>
        )}
      </div>
    </>
  )
}

function Relationships({ root, node }: { root: TreeNode; node: TreeNode }) {
  const par = parentOf(root, node.id)
  const uses = (node.dependencies || []).map(normDep).filter((d) => d.id)
  const receives = par ? (par.edges || []).filter((e) => e.to === node.id) : []
  const feeds = par ? (par.edges || []).filter((e) => e.from === node.id) : []
  const usedBy = flatten(root).filter((m) => m !== node && (m.dependencies || []).some((d) => normDep(d).id === node.id))
  const compose = node.is_leaf === false ? (node.children || []).map((c) => c.id) : null

  return (
    <div className="sec">
      <h4>Relationships</h4>
      <div className="rel"><span className="l">part of</span>{par ? <b className="mono">{par.id}</b> : <span className="faint">root goal</span>}</div>
      <div className="rel"><span className="l">composed of</span>{compose ? <span className="mono">{compose.join(', ')}</span> : <span className="faint">leaf — does the work itself</span>}</div>
      <div className="rel"><span className="l">receives</span>{receives.length ? receives.map((e, i) => <span key={i} className="mono"><b className="dfv">{e.name || 'value'}</b> ← {e.from}{i < receives.length - 1 ? '; ' : ''}</span>) : <span className="faint">—</span>}</div>
      <div className="rel"><span className="l">feeds</span>{feeds.length ? feeds.map((e, i) => <span key={i} className="mono">→ {e.to}{e.name ? <span className="dfv"> ({e.name})</span> : null}{i < feeds.length - 1 ? '; ' : ''}</span>) : <span className="faint">—</span>}</div>
      <div className="rel"><span className="l">uses</span>{uses.length ? uses.map((d, i) => <span key={i} className="mono"><b className="uv">{d.id}</b> as {d.name}{i < uses.length - 1 ? '; ' : ''}</span>) : <span className="faint">self-contained</span>}</div>
      <div className="rel"><span className="l">used by</span>{usedBy.length ? <span className="mono">{usedBy.map((m) => m.id).join(', ')}</span> : <span className="faint">—</span>}</div>
      <div className="rel-help"><b className="dfv">data flow</b> — one stage's output becomes the next's input (a pipe). <b className="uv">uses</b> — calls it as a held tool.</div>
    </div>
  )
}

function WireEditors({ root, node }: { root: TreeNode; node: TreeNode }) {
  const mutate = useStore((s) => s.mutate)
  const par = parentOf(root, node.id)
  const [efFrom, setEfFrom] = useState('')
  const [efTo, setEfTo] = useState('')
  const [efName, setEfName] = useState('')
  const [udId, setUdId] = useState('')
  const [udName, setUdName] = useState('')
  const children = node.children || []
  const sibs = par ? (par.children || []).map((c) => c.id).filter((id) => id !== node.id) : []
  const uses = (node.dependencies || []).map(normDep).filter((d) => d.id)

  return (
    <>
      {node.is_leaf === false && (
        <div className="sec">
          <h4>Wire children · data flow</h4>
          {(node.edges || []).length ? (node.edges || []).map((e, i) => (
            <div key={i} className="edge-row">
              <span className="mono">{e.from} <span className="dfv">→ {e.name || 'value'} →</span> {e.to}</span>
              <button className="tiny danger" onClick={() => mutate((t) => { findNode(t, node.id)!.edges!.splice(i, 1) }, { pushUndo: true })}>✕</button>
            </div>
          )) : <span className="faint">no data-flow links</span>}
          <div className="edge-row" style={{ marginTop: 7 }}>
            <select value={efFrom} onChange={(e) => setEfFrom(e.target.value)}><option value="">from…</option>{children.map((c) => <option key={c.id} value={c.id}>{c.id}</option>)}</select>
            <span>→</span>
            <select value={efTo} onChange={(e) => setEfTo(e.target.value)}><option value="">to…</option>{children.map((c) => <option key={c.id} value={c.id}>{c.id}</option>)}</select>
            <input className="mono" placeholder="value" style={{ width: 70 }} value={efName} onChange={(e) => setEfName(e.target.value)} />
            <button className="tiny" onClick={() => {
              if (!efFrom || !efTo || efFrom === efTo) return
              mutate((t) => { const n = findNode(t, node.id)!; n.edges = n.edges || []; if (!n.edges.some((e) => e.from === efFrom && e.to === efTo)) n.edges.push({ from: efFrom, to: efTo, name: efName.trim() || 'value' }) }, { pushUndo: true })
              setEfFrom(''); setEfTo(''); setEfName('')
            }}>+ link</button>
          </div>
        </div>
      )}
      {par && sibs.length > 0 && (
        <div className="sec">
          <h4>Uses · held capabilities</h4>
          {uses.length ? uses.map((d, i) => (
            <div key={i} className="edge-row">
              <span className="mono"><b className="uv">{d.id}</b> as {d.name}</span>
              <button className="tiny danger" onClick={() => mutate((t) => { findNode(t, node.id)!.dependencies!.splice(i, 1) }, { pushUndo: true })}>✕</button>
            </div>
          )) : <span className="faint">holds nothing</span>}
          <div className="edge-row" style={{ marginTop: 7 }}>
            <select value={udId} onChange={(e) => setUdId(e.target.value)}><option value="">sibling…</option>{sibs.map((s) => <option key={s} value={s}>{s}</option>)}</select>
            <span>as</span>
            <input className="mono" placeholder="name" style={{ width: 80 }} value={udName} onChange={(e) => setUdName(e.target.value)} />
            <button className="tiny" onClick={() => {
              if (!udId) return
              mutate((t) => { const n = findNode(t, node.id)!; n.dependencies = n.dependencies || []; if (!n.dependencies.some((d) => normDep(d).id === udId)) n.dependencies.push({ name: udName.trim() || udId, id: udId }) }, { pushUndo: true })
              setUdId(''); setUdName('')
            }}>+ uses</button>
          </div>
        </div>
      )}
    </>
  )
}

export default function Inspector() {
  const tree = useStore((s) => s.tree)!
  const selectedId = useStore((s) => s.selectedId)
  const editMode = useStore((s) => s.editMode)
  const config = useStore((s) => s.config)
  const select = useStore((s) => s.select)
  const setEditMode = useStore((s) => s.setEditMode)
  const planNode = useStore((s) => s.planNode)
  const vibeNode = useStore((s) => s.vibeNode)
  const addChild = useStore((s) => s.addChild)
  const markLeaf = useStore((s) => s.markLeaf)
  const deleteNode = useStore((s) => s.deleteNode)
  const jobRunning = useStore((s) => s.job.status === 'running')

  const n = selectedId ? findNode(tree, selectedId) : null
  if (!n) return null

  const kind = n.is_leaf === true ? 'leaf' : n.is_leaf === false ? 'internal' : 'unplanned'
  const mk = nodeKind(n)
  const depth = depthOf(tree, n.id)
  const canDecompose = depth < (config?.max_depth ?? 3)
  const ext = n.external || {}
  const adapter = mk === 'adapter'

  return (
    <div className="inspector glass">
      <div className="insp-head">
        <div className="row1">
          <span className="insp-id">{n.id}</span>
          <span className={`badge ${kind}`}>{kind === 'internal' ? `${(n.children || []).length} children` : kind}</span>
          <span className={`badge ${mk}`}>{mk}</span>
          <button className="ghost insp-close" onClick={() => select(null)} title="Close">✕</button>
        </div>
        <div className="sub">
          <span className={'dot'} style={{ background: `var(--${n.status})` }} />
          {n.status} · depth {depth}{n.lane ? ` · ${n.lane}` : ''}
        </div>
      </div>

      <div className="insp-scroll">
        <CardsSection />
        <div className="act">
          <p className="lead">Plan / decompose <b>{n.id}</b></p>
          <div className="rowbtns">
            <button className="primary sm" disabled={!canDecompose || jobRunning} onClick={() => planNode(n.id)}>Plan layer</button>
            <button className="sm" disabled={!canDecompose || jobRunning} onClick={() => vibeNode(n.id)}>Vibe ✦</button>
          </div>
          <div className="rowbtns" style={{ marginTop: 8 }}>
            <button className="sm" onClick={() => addChild(n.id)}>+ Child</button>
            <button className="sm" onClick={() => markLeaf(n.id)}>Mark leaf</button>
            <button className="sm danger" disabled={n.id === tree.id} onClick={() => { if (confirm(`Delete "${n.id}"?`)) deleteNode(n.id) }}>Delete</button>
          </div>
        </div>

        {editMode ? (
          <EditForm node={n} />
        ) : (
          <>
            <ArtifactsView node={n} />
            <div className="sec"><h4>Purpose</h4><p className="purpose">{n.description || '(no description yet)'}</p></div>
            <div className="sec">
              <h4>Interface</h4>
              {(n.operations || []).length ? (n.operations || []).map((op, i) => <OpView key={i} op={op} />) : <span className="faint">no operations defined</span>}
            </div>
            {adapter && (
              <div className="sec">
                <h4>External adapter</h4>
                <div className="rel"><span className="l">provider</span><b>{ext.provider || 'external'}</b></div>
                <div className="rel"><span className="l">unit tests</span><span>{ext.mock_policy || 'mock provider I/O'}</span></div>
                <div className="rel"><span className="l">live smoke</span><span>{ext.live_smoke ? 'allowed' : 'off by default'}</span></div>
                <div className="rel-help">Adapters isolate API/network/file boundaries. Unit tests fake provider responses; live calls belong in separate smoke tests.</div>
              </div>
            )}
            <Relationships root={tree} node={n} />
            <WireEditors root={tree} node={n} />
            <div className="sec"><button className="sm" onClick={() => setEditMode(true)}>✎ Edit contract</button></div>
          </>
        )}
      </div>
    </div>
  )
}

function depthOf(root: TreeNode, id: string): number {
  let d = 0
  const walk = (n: TreeNode, depth: number) => { if (n.id === id) { d = depth; return } ;(n.children || []).forEach((c) => walk(c, depth + 1)) }
  walk(root, 0)
  return d
}
