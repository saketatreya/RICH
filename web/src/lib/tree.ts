import type { Card, Dep, Kind, TreeNode } from './types'

export const clone = <T,>(v: T): T => JSON.parse(JSON.stringify(v))

export function flatten(n: TreeNode | null, out: TreeNode[] = []): TreeNode[] {
  if (!n) return out
  out.push(n)
  ;(n.children || []).forEach((c) => flatten(c, out))
  return out
}

export function findNode(n: TreeNode | null, id: string): TreeNode | null {
  if (!n) return null
  if (n.id === id) return n
  for (const c of n.children || []) {
    const hit = findNode(c, id)
    if (hit) return hit
  }
  return null
}

export function parentOf(root: TreeNode | null, id: string): TreeNode | null {
  if (!root) return null
  let res: TreeNode | null = null
  const walk = (n: TreeNode) => {
    for (const c of n.children || []) {
      if (c.id === id) res = n
      else walk(c)
    }
  }
  walk(root)
  return res
}

export const nodeKind = (n: TreeNode): Kind =>
  (n.kind || (n.stateful ? 'stateful' : 'pure')) as Kind

export const normDep = (d: Dep | string): Dep =>
  typeof d === 'object' ? { name: d.name || d.id, id: d.id || d.name } : { name: d, id: d }

export function normalizeNode(n: TreeNode): TreeNode {
  n.kind = nodeKind(n)
  n.external = n.external || {}
  n.children = n.children || []
  n.edges = n.edges || []
  n.dependencies = n.dependencies || []
  n.operations = n.operations || []
  n.lane = n.lane || ''
  if (n.kind === 'adapter')
    n.external = { provider: '', mock_policy: 'unit_tests_mock_provider', live_smoke: false, ...n.external }
  n.children.forEach(normalizeNode)
  return n
}

export const unplannedNodes = (root: TreeNode | null) =>
  flatten(root).filter((n) => n.is_leaf === null)

export const blockingCards = (cards: Card[]) => cards.filter((c) => c.severity === 'error')

export const buildReady = (root: TreeNode | null, cards: Card[]) =>
  !!root && unplannedNodes(root).length === 0 && blockingCards(cards).length === 0

export function uniqueId(root: TreeNode, base: string): string {
  const ids = new Set(flatten(root).map((n) => n.id))
  let id = base
  let i = 2
  while (ids.has(id)) id = `${base}_${i++}`
  return id
}

export function renameNode(root: TreeNode, n: TreeNode, rawId: string): string {
  const nid = uniqueId(root, rawId)
  const old = n.id
  const par = parentOf(root, old)
  if (par) (par.edges || []).forEach((e) => {
    if (e.from === old) e.from = nid
    if (e.to === old) e.to = nid
  })
  flatten(root).forEach((m) =>
    (m.dependencies || []).forEach((d) => {
      if (typeof d === 'object' && d.id === old) d.id = nid
    }),
  )
  n.id = nid
  return nid
}
