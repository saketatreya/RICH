import { create } from 'zustand'
import { api } from './lib/api'
import type { Card, Config, HistoryItem, TreeNode, Transaction } from './lib/types'
import {
  buildReady,
  clone,
  findNode,
  flatten,
  normalizeNode,
  parentOf,
  uniqueId,
  renameNode as renameInTree,
} from './lib/tree'

type JobStatus = 'idle' | 'running' | 'done' | 'error'

interface JobState {
  kind: string
  status: JobStatus
  log: string[]
  result: any
  activeNode: string | null
}

interface State {
  config: Config | null
  tree: TreeNode | null
  selectedId: string | null
  cards: Card[]
  history: HistoryItem[]
  undoStack: TreeNode[]
  job: JobState
  projectPath: string
  editMode: boolean
  briefOpen: boolean
  liveStatuses: Record<string, string>
  vibeDraft: { prompt: string; nodeId: string | null } | null
  pendingVibe: { tree: TreeNode; transaction: Transaction; before: TreeNode } | null

  setVibeDraft: (d: { prompt: string; nodeId: string | null } | null) => void
  boot: () => Promise<void>
  select: (id: string | null) => void
  setEditMode: (b: boolean) => void
  setBriefOpen: (b: boolean) => void
  setTree: (t: TreeNode | null, opts?: { pushUndo?: boolean; save?: boolean; select?: string | null }) => void
  mutate: (fn: (t: TreeNode) => void, opts?: { pushUndo?: boolean }) => void
  saveProject: () => Promise<void>
  refreshCards: () => Promise<void>
  undo: () => void

  createRoot: (id: string, description: string) => void
  applyProposalTree: (next: TreeNode, transaction: Transaction) => void
  reset: () => void
  addChild: (parentId: string) => void
  deleteNode: (id: string) => void
  markLeaf: (id: string) => void
  renameNode: (id: string, newId: string) => void

  runJob: (kind: 'plan' | 'vibe' | 'build', start: () => Promise<any>) => Promise<void>
  planNode: (id: string) => Promise<void>
  vibeNode: (id: string) => Promise<void>
  build: (nodeId?: string | null) => Promise<void>
  previewVibe: (prompt: string, nodeId: string | null) => Promise<void>
  applyVibe: () => void
  discardVibe: () => void
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))
const idleJob = (): JobState => ({ kind: '', status: 'idle', log: [], result: null, activeNode: null })

export const useStore = create<State>()((set, get) => ({
  config: null,
  tree: null,
  selectedId: null,
  cards: [],
  history: [],
  undoStack: [],
  job: idleJob(),
  projectPath: '',
  editMode: false,
  briefOpen: false,
  liveStatuses: {},
  vibeDraft: null,
  pendingVibe: null,

  setVibeDraft: (d) => set({ vibeDraft: d }),

  boot: async () => {
    try {
      const cfg = await api.config()
      set({ config: cfg, projectPath: cfg.project_path || '' })
    } catch {}
    try {
      const doc = await api.project()
      if (doc.tree) {
        normalizeNode(doc.tree)
        set({ tree: doc.tree, history: doc.history || [], selectedId: doc.tree.id, briefOpen: false })
        get().refreshCards()
      } else {
        set({ briefOpen: true })
      }
    } catch {
      set({ briefOpen: true })
    }
  },

  select: (id) => set({ selectedId: id, editMode: false }),
  setEditMode: (b) => set({ editMode: b }),
  setBriefOpen: (b) => set({ briefOpen: b }),

  setTree: (t, opts = {}) => {
    const prev = get().tree
    if (t) normalizeNode(t)
    set((s) => ({
      tree: t,
      undoStack: opts.pushUndo && prev ? [...s.undoStack, clone(prev)] : s.undoStack,
      selectedId: opts.select !== undefined ? opts.select : s.selectedId,
    }))
    if (opts.save !== false) get().saveProject()
    get().refreshCards()
  },

  mutate: (fn, opts = {}) => {
    const t = get().tree
    if (!t) return
    const next = clone(t)
    fn(next)
    get().setTree(next, { pushUndo: opts.pushUndo })
  },

  saveProject: async () => {
    const { tree, history } = get()
    try {
      const res = await api.saveProject(tree, history)
      if (res?.path) set({ projectPath: res.path })
    } catch {}
  },

  refreshCards: async () => {
    const { tree } = get()
    if (!tree) {
      set({ cards: [] })
      return
    }
    try {
      const res = await api.validate(tree)
      set({ cards: res.cards || [] })
    } catch {}
  },

  undo: () => {
    const stack = get().undoStack
    if (!stack.length) return
    const prev = stack[stack.length - 1]
    set({ undoStack: stack.slice(0, -1), tree: prev })
    get().saveProject()
    get().refreshCards()
  },

  createRoot: (id, description) => {
    const root: TreeNode = {
      id,
      description,
      kind: 'pure',
      external: {},
      stateful: false,
      operations: [{ name: 'run', inputs: { input: 'string' }, outputs: { result: 'string' }, errors: [] }],
      dependencies: [],
      is_leaf: null,
      children: [],
      edges: [],
      status: 'unplanned',
    }
    set({ tree: root, selectedId: id, editMode: false, briefOpen: false, undoStack: [], history: [] })
    get().saveProject()
    get().refreshCards()
  },

  applyProposalTree: (next, transaction) => {
    const before = get().tree
    normalizeNode(next)
    set((s) => ({
      tree: next,
      undoStack: before ? [...s.undoStack, clone(before)] : s.undoStack,
      selectedId: next.id,
      briefOpen: false,
      editMode: false,
      history: [{ ts: Date.now() / 1000, transaction, before: before || undefined, after: clone(next), proposal: true }, ...s.history].slice(0, 50),
    }))
    get().saveProject()
    get().refreshCards()
  },

  reset: () => {
    set({
      tree: null,
      selectedId: null,
      editMode: false,
      cards: [],
      history: [],
      undoStack: [],
      briefOpen: true,
      job: idleJob(),
      liveStatuses: {},
    })
    get().saveProject()
  },

  addChild: (parentId) => {
    const t = get().tree
    if (!t) return
    const next = clone(t)
    const parent = findNode(next, parentId)!
    parent.is_leaf = false
    parent.children = parent.children || []
    parent.edges = parent.edges || []
    const id = uniqueId(next, `${parent.id}_part`)
    parent.children.push({
      id,
      description: '',
      kind: 'pure',
      external: {},
      operations: [],
      dependencies: [],
      stateful: false,
      is_leaf: null,
      children: [],
      edges: [],
      status: 'unplanned',
    })
    get().setTree(next, { pushUndo: true })
    set({ selectedId: id, editMode: true })
  },

  deleteNode: (id) => {
    const t = get().tree
    if (!t || id === t.id) return
    const next = clone(t)
    const par = parentOf(next, id)!
    par.children = (par.children || []).filter((c) => c.id !== id)
    par.edges = (par.edges || []).filter((e) => e.from !== id && e.to !== id)
    if (!par.children.length) {
      par.is_leaf = null
      par.edges = []
    }
    get().setTree(next, { pushUndo: true })
    set({ selectedId: par.id, editMode: false })
  },

  markLeaf: (id) => {
    get().mutate((t) => {
      const n = findNode(t, id)!
      n.is_leaf = true
      n.children = []
      n.edges = []
      n.status = 'planned'
    }, { pushUndo: true })
  },

  renameNode: (id, newId) => {
    const t = get().tree
    if (!t) return
    const next = clone(t)
    const n = findNode(next, id)!
    const nid = renameInTree(next, n, newId)
    set({ selectedId: get().selectedId === id ? nid : get().selectedId })
    get().setTree(next, { pushUndo: true })
  },

  runJob: async (kind, start) => {
    if (get().job.status === 'running') return
    set({ job: { kind, status: 'running', log: [], result: null, activeNode: null } })
    let startRes: any
    try {
      startRes = await start()
    } catch (e: any) {
      set({ job: { kind, status: 'error', log: [`✗ ${e}`], result: null, activeNode: null } })
      return
    }
    if (startRes?.error) {
      set((s) => ({ job: { ...s.job, status: 'error', log: [`✗ ${startRes.error}`] } }))
      return
    }
    const jid = startRes.job_id
    let since = 0
    while (true) {
      await sleep(700)
      let snap: any
      try {
        snap = await api.job(jid, since)
      } catch {
        continue
      }
      if (snap.log?.length) {
        // track the active module from engine log lines like "  [node_id] attempt 1/3 ..."
        let active = get().job.activeNode
        for (const line of snap.log as string[]) {
          const m = /\[([a-z0-9_]+)\]/i.exec(line)
          if (m && m[1] !== 'build') active = m[1]
        }
        set((s) => ({ job: { ...s.job, log: [...s.job.log, ...snap.log], activeNode: active } }))
      }
      since = snap.log_len ?? since
      // live per-module status during a build
      if (kind === 'build') {
        try {
          const st = await api.statuses()
          set({ liveStatuses: st.statuses || {} })
        } catch {}
      }
      if (snap.status !== 'running') {
        set((s) => ({ job: { ...s.job, status: snap.status === 'error' ? 'error' : 'done', result: snap.result } }))
        return
      }
    }
  },

  planNode: async (id) => {
    const { tree } = get()
    if (!tree) return
    await get().runJob('plan', () => api.plan(tree, id))
    const res = get().job.result
    if (res?.tree) {
      mergePositions(get().tree, res.tree)
      set({ tree: res.tree, cards: res.cards || get().cards })
      get().saveProject()
      get().refreshCards()
    }
  },

  vibeNode: async (id) => {
    const { tree } = get()
    if (!tree) return
    await get().runJob('vibe', () => api.vibe(tree, id))
    const res = get().job.result
    if (res?.tree) {
      mergePositions(get().tree, res.tree)
      set({ tree: res.tree, cards: res.cards || get().cards })
      get().saveProject()
      get().refreshCards()
    }
  },

  build: async (nodeId) => {
    const { tree } = get()
    if (!tree) return
    set({ liveStatuses: {} })
    await get().runJob('build', () => api.build(tree, nodeId))
    const res = get().job.result
    if (res?.statuses) {
      const next = clone(get().tree!)
      flatten(next).forEach((n) => {
        if (res.statuses[n.id]) n.status = res.statuses[n.id]
      })
      const summary = [`— build ${res.ok ? 'OK' : 'FAILED'} · ${res.calls} LLM call${res.calls === 1 ? '' : 's'} —`]
      if (res.error) summary.push(`error: ${res.error}`)
      const out = (res.run_stdout || '').trim() || (res.run_stderr || '').trim()
      if (out) summary.push(`deliverable (exit ${res.run_returncode}):`, out)
      set((s) => ({ tree: next, cards: res.cards || s.cards, liveStatuses: {}, job: { ...s.job, log: [...s.job.log, ...summary] } }))
      get().saveProject()
    }
  },

  previewVibe: async (prompt, nodeId) => {
    const { tree, history } = get()
    if (!tree || !prompt.trim()) return
    set({ job: { kind: 'vibe-edit', status: 'running', log: [], result: null, activeNode: null } })
    try {
      const res = await api.vibeEdit(tree, prompt, nodeId, history, true)
      if (res.error) {
        set((s) => ({ job: { ...s.job, status: 'error', log: [`✗ ${res.error}`] } }))
        return
      }
      mergePositions(tree, res.tree)
      const src = res.transaction?.source ? ` [${res.transaction.source}]` : ''
      set((s) => ({
        pendingVibe: { tree: res.tree, transaction: res.transaction, before: clone(tree) },
        cards: res.cards || s.cards,
        vibeDraft: null,
        job: {
          ...s.job,
          status: 'done',
          log: [`vibe${src} (preview): ${res.transaction?.summary || res.transaction?.label || 'review the diff'}`],
        },
      }))
    } catch (e: any) {
      set((s) => ({ job: { ...s.job, status: 'error', log: [`✗ ${e}`] } }))
    }
  },

  applyVibe: () => {
    const pv = get().pendingVibe
    if (!pv) return
    const entry: HistoryItem = { ts: Date.now() / 1000, transaction: pv.transaction, before: pv.before, after: clone(pv.tree) }
    set((s) => ({
      tree: pv.tree,
      undoStack: [...s.undoStack, pv.before],
      history: [entry, ...s.history].slice(0, 50),
      pendingVibe: null,
      selectedId: findNode(pv.tree, s.selectedId || '') ? s.selectedId : pv.tree.id,
    }))
    get().saveProject()
    get().refreshCards()
  },

  discardVibe: () => {
    set({ pendingVibe: null })
    get().refreshCards()
  },
}))

// preserve manual x/y across a server-returned tree
function mergePositions(oldTree: TreeNode | null, srv: TreeNode) {
  if (!oldTree) return
  const pos: Record<string, { x?: number; y?: number }> = {}
  flatten(oldTree).forEach((n) => {
    if (typeof n.x === 'number') pos[n.id] = { x: n.x, y: n.y }
  })
  flatten(srv).forEach((n) => {
    if (pos[n.id]) {
      n.x = pos[n.id].x
      n.y = pos[n.id].y
    }
  })
}

export { buildReady }
