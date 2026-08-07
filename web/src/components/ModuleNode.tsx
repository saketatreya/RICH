import { Handle, Position } from '@xyflow/react'
import type { TreeNode } from '../lib/types'
import { nodeKind } from '../lib/tree'

const STATUS_COLORS: Record<string, string> = {
  unplanned: 'var(--unplanned)',
  planned: 'var(--planned)',
  verified: 'var(--verified)',
  failed: 'var(--failed)',
  building: 'var(--building)',
}

export interface ModuleNodeData {
  node: TreeNode
  selected: boolean
  building: boolean
  status: string
  [key: string]: unknown
}

export default function ModuleNode({ data }: { data: ModuleNodeData }) {
  const n = data.node
  const k = nodeKind(n)
  const status = data.status || n.status || 'unplanned'
  const color = STATUS_COLORS[status] || 'var(--unplanned)'
  const childWord =
    n.is_leaf === true ? 'leaf' : n.is_leaf === false ? `${(n.children || []).length} sub` : 'plan it'
  const ops = (n.operations || []).length
  const cls = ['rf-node', data.selected ? 'sel' : '', n.is_leaf === null ? 'unplanned' : '', data.building ? 'building' : '']
    .filter(Boolean)
    .join(' ')

  return (
    <div className={cls} title={n.description || n.id}>
      <Handle id="t" type="target" position={Position.Top} className="rf-handle" />
      <Handle id="l" type="target" position={Position.Left} className="rf-handle" />
      <Handle id="r" type="source" position={Position.Right} className="rf-handle" />
      <Handle id="b" type="source" position={Position.Bottom} className="rf-handle" />
      <Handle id="bt" type="target" position={Position.Bottom} className="rf-handle" style={{ left: '35%' }} />

      <span className="spine" style={{ background: color, boxShadow: `0 0 12px ${color}` }} />
      <div className="nrow">
        <span className="ndot" style={{ background: color, boxShadow: `0 0 8px ${color}` }} />
        <span className="nid">{n.id}</span>
        {n.is_leaf === null && <span className="nq">?</span>}
      </div>
      <div className="nmeta">
        <span className="ntag">{k}</span>
        <span>{childWord}</span>
        <span>· {ops} op{ops === 1 ? '' : 's'}</span>
        {n.lane ? <span>· {n.lane}</span> : null}
      </div>
    </div>
  )
}
