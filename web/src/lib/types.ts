export type Kind = 'pure' | 'stateful' | 'adapter' | 'ui'
export type Status = 'unplanned' | 'planned' | 'verified' | 'failed' | 'building'

export interface Op {
  name: string
  inputs: Record<string, any>
  outputs: Record<string, any>
  errors: string[]
}
export interface Dep { name: string; id: string }
export interface FlowEdge { from: string; to: string; name?: string }

export interface External {
  provider?: string
  mock_policy?: string
  live_smoke?: boolean
}

export interface TreeNode {
  id: string
  description?: string
  kind?: Kind
  external?: External
  stateful?: boolean
  operations?: Op[]
  dependencies?: (Dep | string)[]
  is_leaf: boolean | null
  children?: TreeNode[]
  edges?: FlowEdge[]
  status?: Status
  lane?: string
  x?: number
  y?: number
}

export interface Card {
  severity: 'error' | 'warning' | 'info'
  node_id?: string | null
  title?: string
  message?: string
  action?: string | null
}

export interface TestReport { passed: boolean; failures: string[]; attempts: number }
export interface Artifacts {
  id: string
  status: Status
  reason: string | null
  report: TestReport | null
  src: Record<string, string>
  tests: Record<string, string>
  built: boolean
}

export interface Config {
  max_depth: number
  backend: string
  backend_available: boolean
  project_path: string
}

export interface Transaction {
  label?: string
  summary?: string
  source?: string
  ops?: any[]
}

export interface BuildResult {
  ok: boolean
  error?: string | null
  calls: number
  statuses: Record<string, Status>
  main_path?: string | null
  run_returncode?: number | null
  run_stdout?: string
  run_stderr?: string
  cards?: Card[]
}

export interface HistoryItem {
  ts: number
  transaction?: Transaction
  before?: TreeNode
  after?: TreeNode
  proposal?: boolean
}
