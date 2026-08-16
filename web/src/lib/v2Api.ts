export type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonValue[]
  | { [key: string]: JsonValue }

export interface Health {
  status: 'ok'
  api_version: string
  store_schema_version: number
}

export interface Project {
  id: string
  name: string
  current_revision: number
  created_at: string
  updated_at: string
}

export interface Requirement {
  id: string
  title: string
  statement: string
  kind: string
  priority: string
  source: string
}

export interface BrowserLocator {
  kind: 'role' | 'label' | 'text' | 'test_id' | 'placeholder'
  value: string
  name?: string
  exact?: boolean
}

export interface AcceptanceStep {
  action:
    | 'open_requirement'
    | 'navigate'
    | 'click'
    | 'fill'
    | 'press'
    | 'keyboard'
    | 'reload'
    | 'assert_visible'
    | 'assert_focused'
    | 'assert_text'
    | 'assert_value'
    | 'assert_url'
  locator?: BrowserLocator
  value?: string
}

export interface AcceptanceScenario {
  id: string
  title: string
  given: string[]
  when: string[]
  then: string[]
  requirement_ids: string[]
  oracle: AcceptanceStep[]
}

export interface ProductSpec {
  schema_version: string
  id: string
  name: string
  goal: string
  audiences: string[]
  requirements: Requirement[]
  acceptance_scenarios: AcceptanceScenario[]
  constraints: string[]
  revision: number
  metadata: Record<string, JsonValue>
}

export interface Revision<T = Record<string, JsonValue>> {
  id: string
  project_id: string
  number: number
  kind: string
  schema_version: string
  document: T
  created_at: string
}

export interface Approval {
  id: string
  project_id: string
  run_id: string | null
  gate: string
  status: 'requested' | 'approved' | 'rejected'
  request: Record<string, JsonValue>
  decision: Record<string, JsonValue> | null
  created_at: string
  decided_at: string | null
}

export interface ArchitectureNode {
  id: string
  name: string
  kind: string
  contract_id: string | null
  owned_paths: string[]
  requirement_ids: string[]
  ports: Array<Record<string, JsonValue>>
  metadata?: Record<string, JsonValue>
}

export interface ArchitectureEdge {
  id: string
  kind: string
  source_node_id: string
  target_node_id: string
  source_port_id: string | null
  target_port_id: string | null
  metadata: Record<string, JsonValue>
}

export interface Architecture {
  schema_version: string
  id: string
  project_id: string
  root_node_id: string
  target_pack: string
  nodes: ArchitectureNode[]
  edges: ArchitectureEdge[]
  contracts: Array<Record<string, JsonValue>>
  project_spec_revision: number
  revision: number
  metadata: Record<string, JsonValue>
}

export interface Run {
  id: string
  project_id: string
  status: string
  spec_revision_id: string
  architecture_revision_id: string
  budget: Record<string, JsonValue>
  created_at: string
  updated_at: string
}

export interface CompiledTask {
  task_id: string
  node_id: string
  dependency_ids: string[]
  owned_paths: string[]
}

export interface DurableTask {
  id: string
  run_id: string
  node_id: string
  kind: string
  status: string
  attempt: number
  cache_key: string | null
}

export interface RunArtifact {
  run_id: string
  task_id: string | null
  digest: string
  role: string
  created_at: string
  size: number
  media_type: string
  metadata: Record<string, JsonValue>
}

/** One file inside a `generated-source` artifact, as the worker wrote it. */
export interface GeneratedFile {
  operation: string
  path: string
  size: number
  sha256: string
  content: string
}

export interface GeneratedSource {
  schema_version: string
  source_digest?: string
  summary: string
  files: GeneratedFile[]
}

export interface ArtifactContent<T = JsonValue> {
  artifact: {
    digest: string
    size: number
    media_type: string
    metadata: Record<string, JsonValue>
    attachments: { role: string; task_id: string | null }[]
  }
  content?: T
  content_base64?: string
}

/**
 * One task's source change, as the write-ahead record describes it. The record
 * itself carries digests, not bytes: `journal_digest` addresses the journal
 * artifact, which is where each file's *original* content lives.
 */
export interface SourceTransaction {
  id: string
  run_id: string
  task_id: string | null
  attempt: number
  status: 'prepared' | 'committed' | 'rolled_back'
  resolution: string | null
  journal_digest: string
  generated_digest: string
  created_at: string
  updated_at: string
}

/**
 * The journal artifact. `original` is what was on disk before the write, which
 * is what makes a real before/after diff possible rather than only showing the
 * new file.
 */
export interface SourceJournal {
  schema_version: string
  run_id: string
  task_id: string
  attempt: number
  source_digest: string
  generated_artifact_digest: string
  files: {
    path: string
    operation: string
    intended: { sha256: string; size: number }
    original: {
      existed: boolean
      mode?: number
      size?: number
      sha256?: string
      content_base64?: string
    }
  }[]
}

export interface RunEvent {
  sequence: number
  run_id: string
  task_id: string | null
  event_type: string
  payload: Record<string, JsonValue>
  created_at: string
}

export interface SpecSubmission {
  spec: ProductSpec
  revision: Revision<ProductSpec>
  approval: Approval
}

export interface ArchitectureSubmission {
  architecture: Architecture
  decisions: string[]
  risks: string[]
  revision: Revision<Architecture>
  approval: Approval
}

export interface PreparedRun {
  run: Run
  compiled: {
    architecture_id: string
    target_pack: string
    tasks: CompiledTask[]
    [key: string]: JsonValue | CompiledTask[]
  }
  tasks: DurableTask[]
  plan_artifact_digest: string
}

export interface ScaffoldResult {
  destination: string
  manifest: {
    target_pack: string
    target_pack_version: string
    content_digest: string
    files: Array<{ path: string; digest: string }>
    [key: string]: JsonValue | Array<{ path: string; digest: string }>
  }
  manifest_artifact_digest: string
}

export interface ExecutionStatus {
  run_id: string
  status: string
  active: boolean
}

export interface InterviewAnswers {
  goal: string
  audiences: string[]
  capabilities: Array<{
    id: string
    title: string
    statement: string
    priority?: string
  }>
  quality_constraints: Array<{
    id: string
    title: string
    statement: string
    priority?: string
  }>
  scenarios: Array<{
    id: string
    title: string
    requirement_ids: string[]
    given?: string[]
    when: string[]
    then: string[]
    oracle: AcceptanceStep[]
  }>
  roles?: string[]
  data_policy?: string[]
  integration_failure_policy?: string[]
  concurrency_policy?: string[]
}

export class V2ApiError extends Error {
  status: number
  kind: string
  details: unknown

  constructor(status: number, kind: string, message: string, details: unknown) {
    super(message)
    this.name = 'V2ApiError'
    this.status = status
    this.kind = kind
    this.details = details
  }
}

const newIdempotencyKey = () => {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) {
    return crypto.randomUUID()
  }
  return `rich-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

const retryKeys = new Map<string, string>()

const shortHash = (value: string) => {
  let left = 0x811c9dc5
  let right = 0x9e3779b9
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index)
    left = Math.imul(left ^ code, 0x01000193)
    right = Math.imul(right ^ code, 0x85ebca6b)
  }
  return `${(left >>> 0).toString(16).padStart(8, '0')}${(right >>> 0).toString(16).padStart(8, '0')}`
}

const retryFingerprint = (path: string, init: RequestInit) => {
  const request = `${String(init.method || 'GET').toUpperCase()}\u0000${path}\u0000${String(init.body || '')}`
  return `${path}:${shortHash(request)}`
}

const retryKey = (fingerprint: string) => {
  const existing = retryKeys.get(fingerprint)
  if (existing) return existing
  const storageKey = `rich.v2.retry.${fingerprint}`
  let key: string | null = null
  try {
    key = sessionStorage.getItem(storageKey)
  } catch {
    // Non-browser and privacy-restricted contexts still get in-memory retry safety.
  }
  key ||= newIdempotencyKey()
  retryKeys.set(fingerprint, key)
  try {
    sessionStorage.setItem(storageKey, key)
  } catch {
    // Best-effort persistence; the in-memory value remains stable.
  }
  return key
}

const clearRetryKey = (fingerprint: string) => {
  retryKeys.delete(fingerprint)
  try {
    sessionStorage.removeItem(`rich.v2.retry.${fingerprint}`)
  } catch {
    // Best effort.
  }
}

async function request<T>(
  path: string,
  init: RequestInit = {},
  mutating = false,
): Promise<T> {
  const headers = new Headers(init.headers)
  headers.set('Accept', 'application/json')
  if (init.body) headers.set('Content-Type', 'application/json')
  const fingerprint = mutating ? retryFingerprint(path, init) : ''
  if (mutating) headers.set('Idempotency-Key', retryKey(fingerprint))

  let response: Response
  try {
    response = await fetch(path, { ...init, headers })
  } catch (error) {
    throw new V2ApiError(
      0,
      'ConnectionError',
      'Could not reach the local RICH control-plane API.',
      error,
    )
  }

  const text = await response.text()
  if (mutating) clearRetryKey(fingerprint)
  let payload: any = {}
  if (text) {
    try {
      payload = JSON.parse(text)
    } catch {
      throw new V2ApiError(
        response.status,
        'InvalidResponse',
        'The v2 API returned a non-JSON response.',
        text,
      )
    }
  }
  if (!response.ok) {
    throw new V2ApiError(
      response.status,
      payload.error || 'ApiError',
      payload.message || `Request failed with status ${response.status}.`,
      payload,
    )
  }
  return payload as T
}

const post = <T>(path: string, body: Record<string, unknown>) =>
  request<T>(
    path,
    { method: 'POST', body: JSON.stringify(body) },
    true,
  )

export const v2Api = {
  health: async (): Promise<Health> =>
    (await request<{ status: Health['status']; api_version: string; store_schema_version: number }>(
      '/v2/health',
    )),

  getProject: async (projectId: string): Promise<Project> =>
    (await request<{ project: Project }>(
      `/v2/projects/${encodeURIComponent(projectId)}`,
    )).project,

  createProject: async (projectId: string, name: string): Promise<Project> =>
    (await post<{ project: Project }>('/v2/projects', {
      project_id: projectId,
      name,
    })).project,

  submitSpec: async (
    project: Project,
    answers: InterviewAnswers,
  ): Promise<SpecSubmission> =>
    post<SpecSubmission>(
      `/v2/projects/${encodeURIComponent(project.id)}/spec-submissions`,
      {
        project_name: project.name,
        answers,
        expected_revision: project.current_revision,
      },
    ),

  proposeArchitecture: async (
    project: Project,
    specRevisionId: string,
    specApprovalId: string,
  ): Promise<ArchitectureSubmission> =>
    post<ArchitectureSubmission>(
      `/v2/projects/${encodeURIComponent(project.id)}/architecture-submissions`,
      {
        spec_revision_id: specRevisionId,
        spec_approval_id: specApprovalId,
        expected_revision: project.current_revision,
      },
    ),

  decideApproval: async (
    approvalId: string,
    approved: boolean,
    actor: string,
    reason: string,
  ): Promise<Approval> =>
    (await post<{ approval: Approval }>(
      `/v2/approvals/${encodeURIComponent(approvalId)}/decisions`,
      { approved, actor, reason },
    )).approval,

  getApproval: async (approvalId: string): Promise<Approval> =>
    (await request<{ approval: Approval }>(
      `/v2/approvals/${encodeURIComponent(approvalId)}`,
    )).approval,

  prepareRun: async (
    architectureApprovalId: string,
    budget: Record<string, unknown>,
  ): Promise<PreparedRun> =>
    post<PreparedRun>('/v2/runs', {
      architecture_approval_id: architectureApprovalId,
      budget,
    }),

  getRun: async (runId: string): Promise<Run> =>
    (await request<{ run: Run }>(`/v2/runs/${encodeURIComponent(runId)}`)).run,

  scaffold: async (
    runId: string,
    destination: string,
    packageScope?: string,
  ): Promise<ScaffoldResult> =>
    post<ScaffoldResult>(`/v2/runs/${encodeURIComponent(runId)}/scaffold`, {
      destination,
      ...(packageScope ? { package_scope: packageScope } : {}),
    }),

  startExecution: async (
    runId: string,
    workspace: string,
    architectureApprovalId?: string,
  ): Promise<ExecutionStatus> =>
    (await post<{ execution: ExecutionStatus }>(
      `/v2/runs/${encodeURIComponent(runId)}/executions`,
      {
        workspace,
        ...(architectureApprovalId
          ? { architecture_approval_id: architectureApprovalId }
          : {}),
      },
    )).execution,

  execution: async (runId: string): Promise<ExecutionStatus> =>
    (await request<{ execution: ExecutionStatus }>(
      `/v2/runs/${encodeURIComponent(runId)}/execution`,
    )).execution,

  artifacts: async (runId: string): Promise<RunArtifact[]> =>
    (await request<{ artifacts: RunArtifact[] }>(
      `/v2/runs/${encodeURIComponent(runId)}/artifacts`,
    )).artifacts,

  artifact: async <T = JsonValue>(
    runId: string,
    digest: string,
  ): Promise<ArtifactContent<T>> =>
    request<ArtifactContent<T>>(
      `/v2/runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(digest)}`,
    ),

  tasks: async (runId: string): Promise<DurableTask[]> =>
    (await request<{ tasks: DurableTask[] }>(
      `/v2/runs/${encodeURIComponent(runId)}/tasks`,
    )).tasks,

  sourceTransactions: async (runId: string): Promise<SourceTransaction[]> =>
    (await request<{ source_transactions: SourceTransaction[] }>(
      `/v2/runs/${encodeURIComponent(runId)}/source-transactions`,
    )).source_transactions,

  events: async (runId: string, after = 0): Promise<RunEvent[]> =>
    (await request<{ events: RunEvent[] }>(
      `/v2/runs/${encodeURIComponent(runId)}/events?after=${after}`,
    )).events,
}
