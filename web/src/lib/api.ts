import type { Artifacts, Config, TreeNode, HistoryItem } from './types'

const post = (p: string, body: any) =>
  fetch(p, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then((r) => r.json())

const get = (p: string) => fetch(p).then((r) => r.json())

export const api = {
  config: (): Promise<Config> => get('/api/config'),
  project: (): Promise<{ tree: TreeNode | null; history: HistoryItem[] }> => get('/api/project'),
  node: (id: string): Promise<Artifacts> => get('/api/node?id=' + encodeURIComponent(id)),
  statuses: (): Promise<{ statuses: Record<string, string> }> => get('/api/statuses'),
  job: (id: string, since: number) => get(`/api/job?id=${id}&since=${since}`),

  saveProject: (tree: TreeNode | null, history: HistoryItem[]) =>
    post('/api/project/save', { tree, history }),
  validate: (tree: TreeNode | null) => post('/api/validate', { tree }),
  vibeEdit: (tree: TreeNode, prompt: string, node_id: string | null, history: HistoryItem[], preview?: boolean) =>
    post('/api/vibe-edit', { tree, prompt, node_id, history, preview: !!preview }),
  propose: (brief: any, tree: TreeNode | null) => post('/api/architecture/propose', { brief, tree }),
  suggestBrief: (brief: any, tree: TreeNode | null) =>
    post('/api/architecture/suggest-brief', { brief, tree }),

  // job-backed (return {job_id})
  plan: (tree: TreeNode, node_id: string) => post('/api/plan', { tree, node_id }),
  vibe: (tree: TreeNode, node_id: string) => post('/api/vibe', { tree, node_id }),
  build: (tree: TreeNode, node_id?: string | null) =>
    post('/api/build', node_id ? { tree, node_id } : { tree }),
}
