import { useCallback, useEffect, useMemo } from 'react'
import {
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  ReactFlow,
  applyNodeChanges,
  useEdgesState,
  useNodesState,
  useReactFlow,
  type Edge,
  type Node,
  type NodeChange,
} from '@xyflow/react'
import ModuleNode from './ModuleNode'
import { useStore } from '../store'
import { flatten, normDep } from '../lib/tree'
import type { TreeNode } from '../lib/types'

const HGAP = 230
const VGAP = 150
const nodeTypes = { module: ModuleNode }

// Tidy layout for nodes without manual x/y (post-order centering).
function layout(root: TreeNode): Record<string, { x: number; y: number }> {
  const pos: Record<string, { x: number; y: number }> = {}
  const lx: Record<string, number> = {}
  let next = 0
  const walk = (n: TreeNode, depth: number) => {
    const kids = n.is_leaf === false ? n.children || [] : []
    if (kids.length) {
      kids.forEach((c) => walk(c, depth + 1))
      lx[n.id] = (lx[kids[0].id] + lx[kids[kids.length - 1].id]) / 2
    } else {
      lx[n.id] = next++
    }
    pos[n.id] = { x: lx[n.id] * HGAP, y: depth * VGAP }
  }
  walk(root, 0)
  return pos
}

function buildGraph(
  tree: TreeNode,
  selectedId: string | null,
  liveStatuses: Record<string, string>,
  activeNode: string | null,
): { nodes: Node[]; edges: Edge[] } {
  const all = flatten(tree)
  const byId: Record<string, TreeNode> = {}
  all.forEach((n) => (byId[n.id] = n))
  const auto = layout(tree)

  const nodes: Node[] = all.map((n) => ({
    id: n.id,
    type: 'module',
    position: { x: typeof n.x === 'number' ? n.x : auto[n.id].x, y: typeof n.y === 'number' ? n.y : auto[n.id].y },
    data: {
      node: n,
      selected: n.id === selectedId,
      building: activeNode === n.id,
      status: liveStatuses[n.id] || n.status || 'unplanned',
    },
  }))

  const edges: Edge[] = []
  for (const n of all) {
    if (n.is_leaf === false)
      for (const c of n.children || [])
        edges.push({
          id: `c-${n.id}-${c.id}`,
          source: n.id,
          sourceHandle: 'b',
          target: c.id,
          targetHandle: 't',
          type: 'smoothstep',
          style: { stroke: '#33404f', strokeWidth: 1.6 },
        })
    for (const e of n.edges || []) {
      if (!byId[e.from] || !byId[e.to]) continue
      edges.push({
        id: `d-${e.from}-${e.to}`,
        source: e.from,
        sourceHandle: 'r',
        target: e.to,
        targetHandle: 'l',
        type: 'smoothstep',
        animated: true,
        label: e.name || 'value',
        labelStyle: { fill: '#74ddcf', fontSize: 11, fontFamily: 'ui-monospace, monospace' },
        labelBgStyle: { fill: '#0c1018' },
        style: { stroke: 'var(--df)', strokeWidth: 1.9 },
      })
    }
    for (const d of n.dependencies || []) {
      const dep = normDep(d)
      if (!dep.id || !byId[dep.id] || dep.id === n.id) continue
      edges.push({
        id: `u-${n.id}-${dep.id}`,
        source: n.id,
        sourceHandle: 'b',
        target: dep.id,
        targetHandle: 'bt',
        type: 'smoothstep',
        className: 'edge-uses',
        label: 'uses',
        labelStyle: { fill: '#c4a8ff', fontSize: 10.5, fontFamily: 'ui-monospace, monospace' },
        labelBgStyle: { fill: '#0c1018' },
        style: { stroke: 'var(--uses)', strokeWidth: 1.5 },
      })
    }
  }
  return { nodes, edges }
}

export default function Canvas() {
  const tree = useStore((s) => s.tree)
  const selectedId = useStore((s) => s.selectedId)
  const liveStatuses = useStore((s) => s.liveStatuses)
  const activeNode = useStore((s) => s.job.activeNode)
  const select = useStore((s) => s.select)
  const mutate = useStore((s) => s.mutate)
  const briefOpen = useStore((s) => s.briefOpen)

  const graph = useMemo(
    () => (tree ? buildGraph(tree, selectedId, liveStatuses, activeNode) : { nodes: [], edges: [] }),
    [tree, selectedId, liveStatuses, activeNode],
  )

  const [nodes, setNodes] = useNodesState<Node>(graph.nodes)
  const [edges, setEdges] = useEdgesState<Edge>(graph.edges)
  const rf = useReactFlow()
  const hasSel = !!selectedId

  useEffect(() => setNodes(graph.nodes), [graph.nodes, setNodes])
  useEffect(() => setEdges(graph.edges), [graph.edges, setEdges])

  // re-center the graph in the visible area when the inspector opens/closes
  useEffect(() => {
    const t = setTimeout(() => rf.fitView({ padding: 0.32, duration: 350, maxZoom: 1.1 }), 290)
    return () => clearTimeout(t)
  }, [hasSel, rf])

  const onNodesChange = useCallback(
    (changes: NodeChange[]) => setNodes((nds) => applyNodeChanges(changes, nds)),
    [setNodes],
  )

  const onNodeDragStop = useCallback(
    (_: unknown, node: Node) => {
      mutate((t) => {
        const found = flatten(t).find((n) => n.id === node.id)
        if (found) {
          found.x = Math.round(node.position.x)
          found.y = Math.round(node.position.y)
        }
      })
    },
    [mutate],
  )

  if (!tree) {
    return (
      <div style={{ width: '100%', height: '100%' }}>
        {!briefOpen && (
          <div className="empty-hint"><div className="inner">No project yet — open <b style={{ color: 'var(--ink-dim)' }}>Brief</b> to architect a goal.</div></div>
        )}
      </div>
    )
  }

  const single = flatten(tree).length === 1 && tree.is_leaf === null

  return (
    <div style={{ width: '100%', height: '100%', position: 'relative' }}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodesChange={onNodesChange}
        onNodeDragStop={onNodeDragStop}
        onNodeClick={(_, n) => select(n.id)}
        onPaneClick={() => select(null)}
        fitView
        fitViewOptions={{ padding: 0.35, maxZoom: 1.1 }}
        minZoom={0.3}
        maxZoom={2.2}
        proOptions={{ hideAttribution: true }}
      >
        <Background variant={BackgroundVariant.Dots} color="#222838" gap={26} size={1.4} />
        <Controls showInteractive={false} position="bottom-right" />
        <MiniMap
          pannable
          zoomable
          maskColor="rgba(7,8,12,.72)"
          nodeStrokeWidth={0}
          nodeColor={(n) => {
            const s = (n.data as any)?.status || 'unplanned'
            return ({ verified: '#2fd3a0', failed: '#ff5d6c', planned: '#5b7cfa', building: '#f0b429' } as any)[s] || '#8a93a3'
          }}
          style={{ background: 'rgba(13,15,22,.82)', bottom: 16, left: 16 }}
        />
      </ReactFlow>
      {single && (
        <div className="empty-hint"><div className="inner">Select the module → <b style={{ color: 'var(--ink-dim)' }}>Plan layer</b> or <b style={{ color: 'var(--ink-dim)' }}>Vibe</b> it.</div></div>
      )}
    </div>
  )
}
