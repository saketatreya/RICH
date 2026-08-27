import { useCallback, useMemo } from 'react'
import {
  Background,
  BackgroundVariant,
  Controls,
  Handle,
  Position,
  ReactFlow,
  type Edge,
  type Node,
} from '@xyflow/react'

import type { Architecture, ArchitectureNode, DurableTask } from '../lib/api'

/**
 * The approved architecture, as a graph.
 *
 * v2 could show a human the shape of what it was about to build only as a list
 * of node ids, while the visual canvas next door drew v1's tree. The graph is
 * the better read of both: layers, who may call whom, and — once a run exists —
 * where the work actually is.
 *
 * Read-only on purpose. Edges here are not a suggestion the viewer can drag
 * around: they are the approved architecture, and changing one means revising
 * it through the draft-and-apply loop where a revision gets recorded.
 */

const LAYER_ROW: Record<string, number> = {
  application: 0,
  ui: 1,
  domain: 2,
  data: 3,
  adapter: 3,
  resource: 4,
}

const KIND_COLOR: Record<string, string> = {
  application: 'var(--accent)',
  ui: 'var(--planned)',
  domain: 'var(--uses)',
  data: 'var(--df)',
  adapter: 'var(--building)',
  resource: 'var(--unplanned)',
}

const STATUS_COLOR: Record<string, string> = {
  succeeded: 'var(--verified)',
  running: 'var(--building)',
  verifying: 'var(--building)',
  ready: 'var(--planned)',
  failed: 'var(--failed)',
  blocked: 'var(--failed)',
  canceled: 'var(--unplanned)',
}

const HGAP = 250
const VGAP = 132

interface NodeData extends Record<string, unknown> {
  node: ArchitectureNode
  status: string | null
  selected: boolean
}

function ArchitectureNodeCard({ data }: { data: NodeData }) {
  const { node, status } = data
  const accent = status
    ? STATUS_COLOR[status] || 'var(--unplanned)'
    : KIND_COLOR[node.kind] || 'var(--unplanned)'
  const operations = node.ports.length
  return (
    <div
      className={`rf-node${data.selected ? ' sel' : ''}`}
      title={node.owned_paths.join('\n') || node.name}
    >
      <Handle id="t" type="target" position={Position.Top} className="rf-handle" />
      <Handle id="b" type="source" position={Position.Bottom} className="rf-handle" />
      <span
        className="spine"
        style={{ background: accent, boxShadow: `0 0 12px ${accent}` }}
      />
      <div className="nrow">
        <span
          className="ndot"
          style={{ background: accent, boxShadow: `0 0 8px ${accent}` }}
        />
        <span className="nid">{node.id}</span>
      </div>
      <div className="nmeta">
        <span className="ntag">{node.kind}</span>
        <span>
          {node.requirement_ids.length} req
          {node.requirement_ids.length === 1 ? '' : 's'}
        </span>
        <span>
          · {operations} port{operations === 1 ? '' : 's'}
        </span>
        {status && <span>· {status}</span>}
      </div>
    </div>
  )
}

const nodeTypes = { architecture: ArchitectureNodeCard }

interface Props {
  architecture: Architecture
  tasks?: DurableTask[]
  selected?: string | null
  onSelect?: (nodeId: string) => void
}

export default function ArchitectureGraph({
  architecture,
  tasks = [],
  selected = null,
  onSelect,
}: Props) {
  const statusByNode = useMemo(() => {
    const index: Record<string, string> = {}
    for (const task of tasks) index[task.node_id] = task.status
    return index
  }, [tasks])

  const nodes: Node<NodeData>[] = useMemo(() => {
    // Laid out by layer rather than by declaration order, because the row a
    // node sits in is the one thing about this graph a reader already knows
    // how to read: what may call what, top to bottom.
    const rows: Record<number, ArchitectureNode[]> = {}
    for (const node of architecture.nodes) {
      const row = LAYER_ROW[node.kind] ?? 2
      ;(rows[row] ||= []).push(node)
    }
    const placed: Node<NodeData>[] = []
    for (const [row, members] of Object.entries(rows)) {
      const depth = Number(row)
      const offset = (members.length - 1) / 2
      members.forEach((node, index) => {
        placed.push({
          id: node.id,
          type: 'architecture',
          position: { x: (index - offset) * HGAP, y: depth * VGAP },
          data: {
            node,
            status: statusByNode[node.id] ?? null,
            selected: selected === node.id,
          },
        } as Node<NodeData>)
      })
    }
    return placed
  }, [architecture, statusByNode, selected])

  const edges: Edge[] = useMemo(
    () =>
      architecture.edges.map((edge) => ({
        id: edge.id,
        source: edge.source_node_id,
        target: edge.target_node_id,
        sourceHandle: 'b',
        targetHandle: 't',
        label: edge.kind,
        className: `rf-edge kind-${edge.kind}`,
        animated: edge.kind === 'event',
      })),
    [architecture],
  )

  const handleNodeClick = useCallback(
    (_: unknown, node: Node) => onSelect?.(node.id),
    [onSelect],
  )

  return (
    <div className="v2-graph">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodeClick={handleNodeClick}
        fitView
        proOptions={{ hideAttribution: true }}
        nodesDraggable={false}
        nodesConnectable={false}
        edgesFocusable={false}
        minZoom={0.3}
        maxZoom={1.6}
      >
        <Background variant={BackgroundVariant.Dots} gap={22} size={1} />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  )
}
