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

/** A proposal nothing has recorded yet: review it, then apply or discard. */
export interface ArchitectureDraft {
  architecture: Architecture
  decisions: string[]
  risks: string[]
  source: 'model' | 'planner'
  rationale: string
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

export interface Preview {
  id: string
  run_id: string
  status: string
  source_digest: string
  neon_project_id: string
  created_at: string
  updated_at: string
  progress?: Record<string, JsonValue>
  result?: Record<string, JsonValue> | null
  [key: string]: JsonValue | undefined
}

export interface PreviewSubmission {
  preview: Preview
  approval: Approval
}

export interface PreviewRequestInput {
  sourceDir: string
  neonProjectId: string
  expiresAt: string
  neonBranchName?: string
  vercelProjectId?: string
  vercelTeamId?: string
}

export interface InterviewQuestion {
  id: string
  prompt: string
  answer_kind: string
  rationale: string
}

export interface InterviewNeeds {
  project_id: string
  questions: InterviewQuestion[]
  complete: boolean
}

/** One line of the interview conversation. */
export interface InterviewTurnLine {
  role: 'user' | 'interviewer'
  text: string
  at?: string
  status?: string
}

/** What one interview turn produced, and how. */
export interface InterviewOutcome {
  status: 'complete' | 'questions' | 'partial'
  summary: string
  questions: Array<{ prompt: string; why: string }>
  rejections: string[]
  attempts: number
  source: 'model' | 'form-fallback'
}

/**
 * The draft document as the server keeps it: the conversation, the
 * structured answers so far, and what the last turn said about them.
 */
export interface InterviewDocument {
  transcript?: InterviewTurnLine[]
  answers?: InterviewAnswers | null
  outcome?: InterviewOutcome
}

/** The in-progress interview, kept on the server so a reload loses nothing. */
export interface InterviewDraft {
  project_id: string
  draft_revision: number
  document: InterviewDocument & Record<string, JsonValue | undefined>
  submitted_revision_id: string | null
  created_at: string
  updated_at: string
}

/** What an oracle step may say, from the models that decide it. */
export interface AcceptanceVocabulary {
  actions: Array<{ action: AcceptanceStep['action']; takes: Array<'locator' | 'value'> }>
  locator_kinds: Array<BrowserLocator['kind']>
  roles: string[]
  path_actions: Array<AcceptanceStep['action']>
}

/**
 * Everything needed to pick a project back up, in the shapes the submission
 * calls return -- so restoring state is the same as receiving it the first time.
 */
export interface ProjectState {
  project: Project
  spec: SpecSubmission | null
  architecture: ArchitectureSubmission | null
  runs: Run[]
  prepared: PreparedRun | null
  scaffold: ScaffoldResult | null
  previews: Preview[]
  interview: InterviewDraft | null
  /** Every approved (spec, architecture) pair, newest first. */
  approved_designs: ApprovedDesign[]
}

export interface ApprovedDesign {
  architecture_revision_id: string
  spec_revision_id: string
  approved_at: string | null
  approval_id: string
}

export interface ChangePlan {
  project_id: string
  change: {
    requirements: { added: string[]; removed: string[]; modified: string[] }
    stale: string[]
    directly_stale: string[]
    contract_changed: string[]
    consumers_stale: string[]
    added_nodes: string[]
    removed_nodes: string[]
    reusable: string[]
    notes: string[]
  }
  forgotten?: Record<string, number>
}

export interface ChangeRevisions {
  fromSpec: string
  toSpec: string
  fromArchitecture: string
  toArchitecture: string
}

export interface NodeRebuild {
  project_id: string
  node_id: string
  /** How many remembered generations were dropped for that node. */
  forgotten_generations: number
}

/** What a run has spent against what it was allowed, from its durable events. */
export interface RunUsage {
  run_id: string
  budget: {
    max_model_attempts: number
    max_input_tokens: number
    max_output_tokens: number
    max_cost_usd: string
    max_execution_seconds: number
  }
  used: {
    model_attempts: number
    input_tokens: number
    output_tokens: number
    cost_usd: string
    execution_seconds: number
  } | null
  remaining: {
    model_attempts: number
    input_tokens: number
    output_tokens: number
    cost_usd: string
    execution_seconds: number
  } | null
  recovery_error?: string
}

export interface RunTimeline {
  run_id: string
  lines: Array<{ sequence: number; text: string }>
  settled: boolean
}

export interface ExecutionStatus {
  run_id: string
  status: string
  /** Executing anywhere, per the durable lease -- not merely in this server. */
  active: boolean
  /** True only when this server process is the one running it. */
  owned_here: boolean
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

export class ApiError extends Error {
  status: number
  kind: string
  details: unknown

  constructor(status: number, kind: string, message: string, details: unknown) {
    super(message)
    this.name = 'ApiError'
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
  const storageKey = `rich.retry.${fingerprint}`
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
    sessionStorage.removeItem(`rich.retry.${fingerprint}`)
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
    throw new ApiError(
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
      throw new ApiError(
        response.status,
        'InvalidResponse',
        'The API returned a non-JSON response.',
        text,
      )
    }
  }
  if (!response.ok) {
    throw new ApiError(
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

const put = <T>(path: string, body: Record<string, unknown>) =>
  request<T>(
    path,
    { method: 'PUT', body: JSON.stringify(body) },
    true,
  )

export const api = {
  health: async (): Promise<Health> =>
    (await request<{ status: Health['status']; api_version: string; store_schema_version: number }>(
      '/v1/health',
    )),

  getProject: async (projectId: string): Promise<Project> =>
    (await request<{ project: Project }>(
      `/v1/projects/${encodeURIComponent(projectId)}`,
    )).project,

  listProjects: async (): Promise<Project[]> =>
    (await request<{ projects: Project[] }>('/v1/projects')).projects,

  /** One call restores a project: its latest spec, architecture, run and draft. */
  projectState: async (projectId: string): Promise<ProjectState> =>
    request<ProjectState>(
      `/v1/projects/${encodeURIComponent(projectId)}/state`,
    ),

  getInterview: async (projectId: string): Promise<InterviewDraft | null> =>
    (await request<{ draft: InterviewDraft | null }>(
      `/v1/projects/${encodeURIComponent(projectId)}/interview`,
    )).draft,

  /**
   * Save the in-progress interview. The expected revision is what this tab
   * last saw; a stale one is a conflict, never a silent overwrite.
   */
  saveInterview: async (
    projectId: string,
    document: Record<string, unknown>,
    expectedDraftRevision: number,
  ): Promise<InterviewDraft> =>
    (await put<{ draft: InterviewDraft }>(
      `/v1/projects/${encodeURIComponent(projectId)}/interview`,
      { document, expected_draft_revision: expectedDraftRevision },
    )).draft,

  /** Where a succeeded run's verified release ZIP downloads from. */
  releaseUrl: (runId: string): string =>
    `/v1/runs/${encodeURIComponent(runId)}/release`,

  /**
   * One interview turn: say what you want; get questions back, or a draft
   * specification. Revises the server-side draft under the revision this tab
   * last saw. One bounded model call, so it can take a minute.
   */
  interviewTurn: async (
    projectId: string,
    message: string,
    expectedDraftRevision: number,
  ): Promise<{ draft: InterviewDraft; outcome: InterviewOutcome }> =>
    post(`/v1/projects/${encodeURIComponent(projectId)}/interview-turns`, {
      message,
      expected_draft_revision: expectedDraftRevision,
    }),

  acceptanceVocabulary: async (): Promise<AcceptanceVocabulary> =>
    request<AcceptanceVocabulary>('/v1/vocabulary/acceptance'),

  createProject: async (projectId: string, name: string): Promise<Project> =>
    (await post<{ project: Project }>('/v1/projects', {
      project_id: projectId,
      name,
    })).project,

  submitSpec: async (
    project: Project,
    answers: InterviewAnswers,
  ): Promise<SpecSubmission> =>
    post<SpecSubmission>(
      `/v1/projects/${encodeURIComponent(project.id)}/spec-submissions`,
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
      `/v1/projects/${encodeURIComponent(project.id)}/architecture-submissions`,
      {
        spec_revision_id: specRevisionId,
        spec_approval_id: specApprovalId,
        expected_revision: project.current_revision,
      },
    ),

  draftArchitecture: async (
    project: Project,
    specRevisionId: string,
    specApprovalId: string,
    repair?: string,
  ): Promise<ArchitectureDraft> =>
    post<ArchitectureDraft>(
      `/v1/projects/${encodeURIComponent(project.id)}/architecture-drafts`,
      {
        spec_revision_id: specRevisionId,
        spec_approval_id: specApprovalId,
        ...(repair ? { repair } : {}),
      },
    ),

  reviseArchitecture: async (
    project: Project,
    specRevisionId: string,
    specApprovalId: string,
    architecture: Architecture,
    decisions: string[] = [],
    risks: string[] = [],
  ): Promise<ArchitectureSubmission> =>
    post<ArchitectureSubmission>(
      `/v1/projects/${encodeURIComponent(project.id)}/architecture-revisions`,
      {
        spec_revision_id: specRevisionId,
        spec_approval_id: specApprovalId,
        expected_revision: project.current_revision,
        architecture,
        decisions,
        risks,
      },
    ),

  /**
   * Request a preview. The approval this returns binds the exact source digest
   * that was verified, so a later change cannot ride an earlier decision.
   */
  requestPreview: async (
    runId: string,
    input: PreviewRequestInput,
  ): Promise<PreviewSubmission> =>
    post<PreviewSubmission>(
      `/v1/runs/${encodeURIComponent(runId)}/preview-requests`,
      {
        source_dir: input.sourceDir,
        neon_project_id: input.neonProjectId,
        expires_at: input.expiresAt,
        ...(input.neonBranchName ? { neon_branch_name: input.neonBranchName } : {}),
        ...(input.vercelProjectId
          ? { vercel_project_id: input.vercelProjectId }
          : {}),
        ...(input.vercelTeamId ? { vercel_team_id: input.vercelTeamId } : {}),
      },
    ),

  previews: async (runId: string): Promise<Preview[]> =>
    (
      await request<{ previews: Preview[] }>(
        `/v1/runs/${encodeURIComponent(runId)}/previews`,
      )
    ).previews,

  getPreview: async (previewId: string): Promise<Preview> =>
    (
      await request<{ preview: Preview }>(
        `/v1/previews/${encodeURIComponent(previewId)}`,
      )
    ).preview,

  deployPreview: async (previewId: string, approvalId: string): Promise<Preview> =>
    (
      await post<{ preview: Preview }>(
        `/v1/previews/${encodeURIComponent(previewId)}/deployments`,
        { approval_id: approvalId },
      )
    ).preview,

  destroyPreview: async (previewId: string): Promise<Preview> =>
    (
      await post<{ preview: Preview }>(
        `/v1/previews/${encodeURIComponent(previewId)}/destroy`,
        {},
      )
    ).preview,

  /**
   * Ask what still needs answering, given what has been said so far. Reads and
   * reports; the spec is only created by submitSpec.
   */
  interviewNeeds: async (
    projectId: string,
    projectName: string,
    answers: Partial<InterviewAnswers>,
    limit = 10,
  ): Promise<InterviewNeeds> =>
    post<InterviewNeeds>(
      `/v1/projects/${encodeURIComponent(projectId)}/interview-questions`,
      { project_name: projectName, answers, limit },
    ),

  /**
   * What moving between two approved revisions costs. Reads only: the plan is
   * for looking at, and `applyChange` is the decision.
   */
  planChange: async (
    projectId: string,
    revisions: ChangeRevisions,
  ): Promise<ChangePlan> =>
    post<ChangePlan>(
      `/v1/projects/${encodeURIComponent(projectId)}/change-plans`,
      {
        from_spec_revision_id: revisions.fromSpec,
        to_spec_revision_id: revisions.toSpec,
        from_architecture_revision_id: revisions.fromArchitecture,
        to_architecture_revision_id: revisions.toArchitecture,
      },
    ),

  applyChange: async (
    projectId: string,
    revisions: ChangeRevisions,
  ): Promise<ChangePlan> =>
    post<ChangePlan>(
      `/v1/projects/${encodeURIComponent(projectId)}/change-applications`,
      {
        from_spec_revision_id: revisions.fromSpec,
        to_spec_revision_id: revisions.toSpec,
        from_architecture_revision_id: revisions.fromArchitecture,
        to_architecture_revision_id: revisions.toArchitecture,
      },
    ),

  /**
   * Forget one node's remembered generation so the next run recomputes it
   * rather than replaying what it said last time. Verification is unaffected --
   * it is never reused in the first place.
   */
  rebuildNode: async (
    projectId: string,
    nodeId: string,
    architectureRevisionId?: string,
  ): Promise<NodeRebuild> =>
    (
      await post<{ rebuild: NodeRebuild }>(
        `/v1/projects/${encodeURIComponent(projectId)}/node-rebuilds`,
        {
          node_id: nodeId,
          ...(architectureRevisionId
            ? { architecture_revision_id: architectureRevisionId }
            : {}),
        },
      )
    ).rebuild,

  decideApproval: async (
    approvalId: string,
    approved: boolean,
    actor: string,
    reason: string,
  ): Promise<Approval> =>
    (await post<{ approval: Approval }>(
      `/v1/approvals/${encodeURIComponent(approvalId)}/decisions`,
      { approved, actor, reason },
    )).approval,

  getApproval: async (approvalId: string): Promise<Approval> =>
    (await request<{ approval: Approval }>(
      `/v1/approvals/${encodeURIComponent(approvalId)}`,
    )).approval,

  prepareRun: async (
    architectureApprovalId: string,
    budget: Record<string, unknown>,
  ): Promise<PreparedRun> =>
    post<PreparedRun>('/v1/runs', {
      architecture_approval_id: architectureApprovalId,
      budget,
    }),

  getRun: async (runId: string): Promise<Run> =>
    (await request<{ run: Run }>(`/v1/runs/${encodeURIComponent(runId)}`)).run,

  scaffold: async (
    runId: string,
    destination: string,
    packageScope?: string,
  ): Promise<ScaffoldResult> =>
    post<ScaffoldResult>(`/v1/runs/${encodeURIComponent(runId)}/scaffold`, {
      destination,
      ...(packageScope ? { package_scope: packageScope } : {}),
    }),

  startExecution: async (
    runId: string,
    workspace: string,
    architectureApprovalId?: string,
  ): Promise<ExecutionStatus> =>
    (await post<{ execution: ExecutionStatus }>(
      `/v1/runs/${encodeURIComponent(runId)}/executions`,
      {
        workspace,
        ...(architectureApprovalId
          ? { architecture_approval_id: architectureApprovalId }
          : {}),
      },
    )).execution,

  /**
   * Ask a run to stop at its next checkpoint. Cooperative: the engine unwinds
   * through its own paths rather than being killed mid-write.
   */
  cancelRun: async (runId: string, reason?: string): Promise<void> => {
    await post(`/v1/runs/${encodeURIComponent(runId)}/cancellation`, {
      ...(reason ? { reason } : {}),
    })
  },

  execution: async (runId: string): Promise<ExecutionStatus> =>
    (await request<{ execution: ExecutionStatus }>(
      `/v1/runs/${encodeURIComponent(runId)}/execution`,
    )).execution,

  artifacts: async (runId: string): Promise<RunArtifact[]> =>
    (await request<{ artifacts: RunArtifact[] }>(
      `/v1/runs/${encodeURIComponent(runId)}/artifacts`,
    )).artifacts,

  artifact: async <T = JsonValue>(
    runId: string,
    digest: string,
  ): Promise<ArtifactContent<T>> =>
    request<ArtifactContent<T>>(
      `/v1/runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(digest)}`,
    ),

  tasks: async (runId: string): Promise<DurableTask[]> =>
    (await request<{ tasks: DurableTask[] }>(
      `/v1/runs/${encodeURIComponent(runId)}/tasks`,
    )).tasks,

  sourceTransactions: async (runId: string): Promise<SourceTransaction[]> =>
    (await request<{ source_transactions: SourceTransaction[] }>(
      `/v1/runs/${encodeURIComponent(runId)}/source-transactions`,
    )).source_transactions,

  usage: async (runId: string): Promise<RunUsage> =>
    request<RunUsage>(`/v1/runs/${encodeURIComponent(runId)}/usage`),

  timeline: async (runId: string, after = 0): Promise<RunTimeline> =>
    request<RunTimeline>(
      `/v1/runs/${encodeURIComponent(runId)}/timeline?after=${after}`,
    ),

  events: async (runId: string, after = 0): Promise<RunEvent[]> =>
    (await request<{ events: RunEvent[] }>(
      `/v1/runs/${encodeURIComponent(runId)}/events?after=${after}`,
    )).events,
}
