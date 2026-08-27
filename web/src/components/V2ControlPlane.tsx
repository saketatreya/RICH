import { useEffect, useMemo, useState } from 'react'
import {
  type AcceptanceScenario,
  type Approval,
  type ArchitectureDraft,
  type ArchitectureSubmission,
  type ExecutionStatus,
  type Health,
  type InterviewAnswers,
  type PreparedRun,
  type Project,
  type RunEvent,
  type ScaffoldResult,
  type SpecSubmission,
  V2ApiError,
  v2Api,
} from '../lib/v2Api'
import ArchitectureDraftReview from './ArchitectureDraftReview'
import V2Inspector from './V2Inspector'

type RequirementDraft = {
  id: string
  title: string
  statement: string
}

type ScenarioDraft = {
  id: string
  title: string
  requirementIds: string
  given: string
  when: string
  then: string
  oracle: string
}

type IntentDraft = {
  goal: string
  audiences: string
  capabilities: RequirementDraft[]
  qualityConstraints: RequirementDraft[]
  scenarios: ScenarioDraft[]
  roles: string
  dataPolicy: string
  integrationFailurePolicy: string
  concurrencyPolicy: string
}

type SavedSession = {
  project: Project | null
  spec: SpecSubmission | null
  architecture: ArchitectureSubmission | null
  prepared: PreparedRun | null
  scaffold: ScaffoldResult | null
}

const SESSION_KEY = 'rich.v2.control-plane.session'

const defaultDraft: IntentDraft = {
  goal: 'Give technical founders a trustworthy way to turn approved product intent into a working web application.',
  audiences: 'Technical founders',
  capabilities: [
    {
      id: 'req.workflow',
      title: 'Create a project workflow',
      statement: 'A founder can create a project and review each approval-gated build stage.',
    },
  ],
  qualityConstraints: [
    {
      id: 'req.a11y',
      title: 'Keyboard access',
      statement: 'Every core workflow is operable with a keyboard.',
    },
  ],
  scenarios: [
    {
      id: 'scenario.workflow',
      title: 'Create a project',
      requirementIds: 'req.workflow',
      given: 'A founder has opened the control plane.',
      when: 'They create a project and submit its intent.',
      then: 'The product specification is available for explicit approval.',
      oracle: JSON.stringify([
        { action: 'navigate', value: '/' },
        {
          action: 'fill',
          locator: { kind: 'label', value: 'Project name' },
          value: 'Example project',
        },
        {
          action: 'click',
          locator: { kind: 'role', value: 'button', name: 'Create project' },
        },
        {
          action: 'assert_visible',
          locator: { kind: 'text', value: 'approval', exact: false },
        },
      ], null, 2),
    },
    {
      id: 'scenario.a11y',
      title: 'Complete the workflow with a keyboard',
      requirementIds: 'req.a11y',
      given: 'A founder uses only a keyboard.',
      when: 'They move through the core project workflow.',
      then: 'Every control can be reached, understood, and activated.',
      oracle: JSON.stringify([
        { action: 'navigate', value: '/' },
        { action: 'keyboard', value: 'Tab' },
        {
          action: 'assert_focused',
          locator: { kind: 'label', value: 'Project name' },
        },
      ], null, 2),
    },
  ],
  roles: '',
  dataPolicy: '',
  integrationFailurePolicy: '',
  concurrencyPolicy: '',
}

const emptySession: SavedSession = {
  project: null,
  spec: null,
  architecture: null,
  prepared: null,
  scaffold: null,
}

function restoreSession(): SavedSession {
  try {
    const raw = localStorage.getItem(SESSION_KEY)
    return raw ? { ...emptySession, ...JSON.parse(raw) } : emptySession
  } catch {
    return emptySession
  }
}

const lines = (value: string) =>
  value
    .split('\n')
    .map((item) => item.trim())
    .filter(Boolean)

const commaList = (value: string) =>
  value
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean)

const errorMessage = (error: unknown) =>
  error instanceof V2ApiError
    ? `${error.kind}${error.status ? ` (${error.status})` : ''}: ${error.message}`
    : error instanceof Error
      ? error.message
      : String(error)

const shortId = (value: string | null | undefined) =>
  value ? (value.length > 26 ? `${value.slice(0, 13)}…${value.slice(-8)}` : value) : '—'

const statusClass = (status: string) => {
  if (['ok', 'approved', 'ready', 'completed'].includes(status)) return 'ok'
  if (['rejected', 'failed', 'offline'].includes(status)) return 'bad'
  return 'warn'
}

function ApprovalGate({
  title,
  description,
  approval,
  actor,
  busy,
  onDecision,
}: {
  title: string
  description: string
  approval: Approval
  actor: string
  busy: boolean
  onDecision: (approved: boolean) => void
}) {
  return (
    <section className="v2-gate">
      <div className="v2-gate-icon">{approval.status === 'approved' ? '✓' : '◆'}</div>
      <div className="v2-gate-main">
        <div className="v2-section-title">
          <div>
            <span className="v2-eyebrow">Human authority</span>
            <h3>{title}</h3>
          </div>
          <span className={`chip ${statusClass(approval.status)}`}>{approval.status}</span>
        </div>
        <p>{description}</p>
        <div className="v2-idline" title={approval.id}>
          <span>Approval</span>
          <code>{shortId(approval.id)}</code>
        </div>
        {approval.decision && (
          <div className="v2-decision">
            Decided by <b>{String(approval.decision.actor || 'unknown')}</b>
            {approval.decision.reason ? ` · ${String(approval.decision.reason)}` : ''}
          </div>
        )}
        {approval.status === 'requested' && (
          <div className="v2-actions">
            <button
              className="primary"
              disabled={busy || !actor.trim()}
              onClick={() => onDecision(true)}
            >
              Approve gate
            </button>
            <button
              className="danger"
              disabled={busy || !actor.trim()}
              onClick={() => onDecision(false)}
            >
              Reject
            </button>
          </div>
        )}
      </div>
    </section>
  )
}

function RequirementEditor({
  title,
  note,
  items,
  onChange,
}: {
  title: string
  note: string
  items: RequirementDraft[]
  onChange: (items: RequirementDraft[]) => void
}) {
  const update = (index: number, patch: Partial<RequirementDraft>) =>
    onChange(items.map((item, i) => (i === index ? { ...item, ...patch } : item)))

  return (
    <div className="v2-form-section">
      <div className="v2-form-section-head">
        <div>
          <h4>{title}</h4>
          <p>{note}</p>
        </div>
        <button
          className="tiny ghost"
          onClick={() =>
            onChange([
              ...items,
              { id: `req.${items.length + 1}`, title: '', statement: '' },
            ])
          }
        >
          + Add
        </button>
      </div>
      <div className="v2-editor-list">
        {items.map((item, index) => (
          <div className="v2-editor-card" key={`${item.id}-${index}`}>
            <input
              aria-label={`${title} ${index + 1} id`}
              className="mono"
              value={item.id}
              onChange={(event) => update(index, { id: event.target.value })}
              placeholder="req.stable-id"
            />
            <input
              aria-label={`${title} ${index + 1} title`}
              value={item.title}
              onChange={(event) => update(index, { title: event.target.value })}
              placeholder="Observable capability"
            />
            <textarea
              aria-label={`${title} ${index + 1} statement`}
              value={item.statement}
              onChange={(event) => update(index, { statement: event.target.value })}
              placeholder="Describe behavior a user or test can observe."
            />
            {items.length > 1 && (
              <button
                className="tiny ghost danger v2-remove"
                aria-label={`Remove ${title} ${index + 1}`}
                onClick={() => onChange(items.filter((_, i) => i !== index))}
              >
                Remove
              </button>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}

function ScenarioEditor({
  items,
  onChange,
}: {
  items: ScenarioDraft[]
  onChange: (items: ScenarioDraft[]) => void
}) {
  const update = (index: number, patch: Partial<ScenarioDraft>) =>
    onChange(items.map((item, i) => (i === index ? { ...item, ...patch } : item)))

  return (
    <div className="v2-form-section">
      <div className="v2-form-section-head">
        <div>
          <h4>Acceptance scenarios</h4>
          <p>Every requirement needs a Given/When/Then behavioral oracle.</p>
        </div>
        <button
          className="tiny ghost"
          onClick={() =>
            onChange([
              ...items,
              {
                id: `scenario.${items.length + 1}`,
                title: '',
                requirementIds: '',
                given: '',
                when: '',
                then: '',
                oracle: JSON.stringify([
                  { action: 'navigate', value: '/' },
                  {
                    action: 'assert_visible',
                    locator: { kind: 'role', value: 'heading' },
                  },
                ], null, 2),
              },
            ])
          }
        >
          + Add
        </button>
      </div>
      <div className="v2-editor-list">
        {items.map((item, index) => (
          <div className="v2-editor-card v2-scenario-card" key={`${item.id}-${index}`}>
            <input
              aria-label={`Scenario ${index + 1} id`}
              className="mono"
              value={item.id}
              onChange={(event) => update(index, { id: event.target.value })}
              placeholder="scenario.stable-id"
            />
            <input
              aria-label={`Scenario ${index + 1} title`}
              value={item.title}
              onChange={(event) => update(index, { title: event.target.value })}
              placeholder="Scenario title"
            />
            <input
              aria-label={`Scenario ${index + 1} requirement ids`}
              className="mono"
              value={item.requirementIds}
              onChange={(event) => update(index, { requirementIds: event.target.value })}
              placeholder="req.one, req.two"
            />
            <div className="v2-gwt">
              <label>
                <span>Given</span>
                <textarea
                  value={item.given}
                  onChange={(event) => update(index, { given: event.target.value })}
                  placeholder="One condition per line"
                />
              </label>
              <label>
                <span>When</span>
                <textarea
                  value={item.when}
                  onChange={(event) => update(index, { when: event.target.value })}
                  placeholder="One action per line"
                />
              </label>
              <label>
                <span>Then</span>
                <textarea
                  value={item.then}
                  onChange={(event) => update(index, { then: event.target.value })}
                  placeholder="One observable result per line"
                />
              </label>
            </div>
            <label className="v2-oracle">
              <span>Executable browser oracle · approved JSON steps</span>
              <textarea
                className="mono"
                value={item.oracle}
                onChange={(event) => update(index, { oracle: event.target.value })}
                placeholder='[{"action":"navigate","value":"/"},{"action":"assert_visible","locator":{"kind":"role","value":"heading"}}]'
              />
            </label>
            {items.length > 1 && (
              <button
                className="tiny ghost danger v2-remove"
                aria-label={`Remove scenario ${index + 1}`}
                onClick={() => onChange(items.filter((_, i) => i !== index))}
              >
                Remove
              </button>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}

function ScenarioList({ scenarios }: { scenarios: AcceptanceScenario[] }) {
  return (
    <div className="v2-evidence-grid">
      {scenarios.map((scenario) => (
        <article className="v2-evidence-card" key={scenario.id}>
          <div className="v2-card-top">
            <code>{scenario.id}</code>
            <span>{scenario.requirement_ids.join(', ')}</span>
          </div>
          <h4>{scenario.title}</h4>
          <div className="v2-gwt-read">
            {!!scenario.given.length && <p><b>Given</b> {scenario.given.join(' · ')}</p>}
            <p><b>When</b> {scenario.when.join(' · ')}</p>
            <p><b>Then</b> {scenario.then.join(' · ')}</p>
            <p><b>Oracle</b> {scenario.oracle.length} executable browser steps</p>
          </div>
        </article>
      ))}
    </div>
  )
}

export default function V2ControlPlane() {
  const restored = useMemo(restoreSession, [])
  const [health, setHealth] = useState<Health | null>(null)
  const [connectionError, setConnectionError] = useState('')
  const [project, setProject] = useState<Project | null>(restored.project)
  const [spec, setSpec] = useState<SpecSubmission | null>(restored.spec)
  const [architecture, setArchitecture] = useState<ArchitectureSubmission | null>(
    restored.architecture,
  )
  const [architectureDraft, setArchitectureDraft] =
    useState<ArchitectureDraft | null>(null)
  const [prepared, setPrepared] = useState<PreparedRun | null>(restored.prepared)
  const [scaffold, setScaffold] = useState<ScaffoldResult | null>(restored.scaffold)
  const [events, setEvents] = useState<RunEvent[]>([])
  const [execution, setExecution] = useState<ExecutionStatus | null>(null)
  const [projectId, setProjectId] = useState(restored.project?.id || 'project.rich-demo')
  const [projectName, setProjectName] = useState(restored.project?.name || 'RICH Demo')
  const [draft, setDraft] = useState<IntentDraft>(defaultDraft)
  const [actor, setActor] = useState('founder')
  const [reason, setReason] = useState('Reviewed against the product intent.')
  const [maxAttempts, setMaxAttempts] = useState('20')
  const [maxCost, setMaxCost] = useState('10.00')
  const [destination, setDestination] = useState(
    `rich-${(restored.project?.id || 'demo').split('.').pop()}`,
  )
  const [packageScope, setPackageScope] = useState('@rich-app')
  const [busy, setBusy] = useState('')
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')

  const persist = (
    next: Partial<SavedSession> = {},
    base: SavedSession = { project, spec, architecture, prepared, scaffold },
  ) => {
    const session = { ...base, ...next }
    localStorage.setItem(SESSION_KEY, JSON.stringify(session))
  }

  const runAction = async (label: string, action: () => Promise<void>) => {
    setBusy(label)
    setError('')
    setNotice('')
    try {
      await action()
    } catch (actionError) {
      setError(errorMessage(actionError))
    } finally {
      setBusy('')
    }
  }

  const checkHealth = async () => {
    try {
      const result = await v2Api.health()
      setHealth(result)
      setConnectionError('')
    } catch (healthError) {
      setHealth(null)
      setConnectionError(errorMessage(healthError))
    }
  }

  useEffect(() => {
    checkHealth()
  }, [])

  useEffect(() => {
    if (!prepared?.run.id) return
    let cancelled = false
    const refresh = async () => {
      try {
        const [nextEvents, nextRun, nextExecution] = await Promise.all([
          v2Api.events(prepared.run.id),
          v2Api.getRun(prepared.run.id),
          v2Api.execution(prepared.run.id),
        ])
        if (!cancelled) {
          setEvents(nextEvents)
          setExecution(nextExecution)
          setPrepared((current) =>
            current ? { ...current, run: nextRun } : current,
          )
        }
      } catch {
        // The primary connection banner owns transient API errors.
      }
    }
    refresh()
    const interval = window.setInterval(refresh, 3500)
    return () => {
      cancelled = true
      window.clearInterval(interval)
    }
  }, [prepared?.run.id])

  const resetAfterProject = (nextProject: Project) => {
    setProject(nextProject)
    setSpec(null)
    setArchitecture(null)
    setPrepared(null)
    setScaffold(null)
    setExecution(null)
    setEvents([])
    setProjectId(nextProject.id)
    setProjectName(nextProject.name)
    setDestination(`rich-${nextProject.id.split('.').pop()}`)
    persist(
      { project: nextProject, spec: null, architecture: null, prepared: null, scaffold: null },
      emptySession,
    )
  }

  const createProject = () =>
    runAction('create-project', async () => {
      const result = await v2Api.createProject(projectId.trim(), projectName.trim())
      resetAfterProject(result)
      setNotice(`Created ${result.id}. Intent compilation is ready.`)
    })

  const loadProject = () =>
    runAction('load-project', async () => {
      const result = await v2Api.getProject(projectId.trim())
      if (project?.id === result.id) {
        setProject(result)
        persist({ project: result })
      } else {
        resetAfterProject(result)
      }
      setNotice(`Loaded ${result.id} at revision ${result.current_revision}.`)
    })

  const answers = (): InterviewAnswers => {
    const result: InterviewAnswers = {
      goal: draft.goal.trim(),
      audiences: lines(draft.audiences),
      capabilities: draft.capabilities.map((item) => ({
        id: item.id.trim(),
        title: item.title.trim(),
        statement: item.statement.trim(),
      })),
      quality_constraints: draft.qualityConstraints.map((item) => ({
        id: item.id.trim(),
        title: item.title.trim(),
        statement: item.statement.trim(),
      })),
      scenarios: draft.scenarios.map((item) => ({
        id: item.id.trim(),
        title: item.title.trim(),
        requirement_ids: commaList(item.requirementIds),
        given: lines(item.given),
        when: lines(item.when),
        then: lines(item.then),
        oracle: JSON.parse(item.oracle),
      })),
    }
    const optional: Array<[keyof InterviewAnswers, string]> = [
      ['roles', draft.roles],
      ['data_policy', draft.dataPolicy],
      ['integration_failure_policy', draft.integrationFailurePolicy],
      ['concurrency_policy', draft.concurrencyPolicy],
    ]
    for (const [key, value] of optional) {
      const values = lines(value)
      if (values.length) (result as any)[key] = values
    }
    return result
  }

  const submitSpec = () => {
    if (!project) return
    runAction('submit-spec', async () => {
      const result = await v2Api.submitSpec(project, answers())
      const refreshed = await v2Api.getProject(project.id)
      setProject(refreshed)
      setSpec(result)
      setArchitecture(null)
      setPrepared(null)
      setScaffold(null)
      setExecution(null)
      persist({
        project: refreshed,
        spec: result,
        architecture: null,
        prepared: null,
        scaffold: null,
      })
      setNotice('Intent compiled into a versioned spec. Human approval is now required.')
    })
  }

  const decideSpec = (approved: boolean) => {
    if (!spec) return
    runAction('spec-decision', async () => {
      const approval = await v2Api.decideApproval(spec.approval.id, approved, actor, reason)
      const next = { ...spec, approval }
      setSpec(next)
      persist({ spec: next })
      setNotice(approved ? 'Product specification approved.' : 'Product specification rejected.')
    })
  }

  const draftArchitecture = (repair?: string) => {
    if (!project || !spec) return
    runAction('draft-architecture', async () => {
      const result = await v2Api.draftArchitecture(
        project,
        spec.revision.id,
        spec.approval.id,
        repair,
      )
      setArchitectureDraft(result)
      setNotice(
        result.source === 'model'
          ? 'Architect proposed a decomposition. Nothing is recorded yet — review the change.'
          : 'No architect configured; the deterministic planner proposed this. Nothing is recorded yet.',
      )
    })
  }

  const applyDraft = () => {
    if (!project || !spec || !architectureDraft) return
    runAction('apply-draft', async () => {
      const result = await v2Api.reviseArchitecture(
        project,
        spec.revision.id,
        spec.approval.id,
        architectureDraft.architecture,
        architectureDraft.decisions,
        architectureDraft.risks,
      )
      const refreshed = await v2Api.getProject(project.id)
      setProject(refreshed)
      setArchitecture(result)
      setArchitectureDraft(null)
      setPrepared(null)
      setScaffold(null)
      setExecution(null)
      persist({ project: refreshed, architecture: result, prepared: null, scaffold: null })
      setNotice('Architecture recorded as a new revision. It needs its own approval.')
    })
  }

  const proposeArchitecture = () => {
    if (!project || !spec) return
    runAction('propose-architecture', async () => {
      const result = await v2Api.proposeArchitecture(
        project,
        spec.revision.id,
        spec.approval.id,
      )
      const refreshed = await v2Api.getProject(project.id)
      setProject(refreshed)
      setArchitecture(result)
      setPrepared(null)
      setScaffold(null)
      setExecution(null)
      persist({
        project: refreshed,
        architecture: result,
        prepared: null,
        scaffold: null,
      })
      setNotice('Architecture compiled. Review ownership, risks, and dependency boundaries.')
    })
  }

  const decideArchitecture = (approved: boolean) => {
    if (!architecture) return
    runAction('architecture-decision', async () => {
      const approval = await v2Api.decideApproval(
        architecture.approval.id,
        approved,
        actor,
        reason,
      )
      const next = { ...architecture, approval }
      setArchitecture(next)
      persist({ architecture: next })
      setNotice(approved ? 'Architecture approved.' : 'Architecture rejected.')
    })
  }

  const prepareRun = () => {
    if (!architecture) return
    runAction('prepare-run', async () => {
      const result = await v2Api.prepareRun(architecture.approval.id, {
        max_model_attempts: Number(maxAttempts),
        max_input_tokens: Number(maxAttempts) * 32_000,
        max_output_tokens: Number(maxAttempts) * 8_000,
        max_cost_usd: maxCost,
        max_execution_seconds: Number(maxAttempts) * 120,
      })
      setPrepared(result)
      setScaffold(null)
      setExecution(null)
      persist({ prepared: result, scaffold: null })
      setNotice(`Durable run ${shortId(result.run.id)} is ready with ${result.tasks.length} tasks.`)
    })
  }

  const scaffoldRun = () => {
    if (!prepared) return
    runAction('scaffold', async () => {
      const result = await v2Api.scaffold(
        prepared.run.id,
        destination.trim(),
        packageScope.trim() || undefined,
      )
      setScaffold(result)
      persist({ scaffold: result })
      setNotice(`Scaffold written to ${result.destination}.`)
      setEvents(await v2Api.events(prepared.run.id))
    })
  }

  const executeRun = () => {
    if (!prepared || !scaffold) return
    runAction('execute-run', async () => {
      const result = await v2Api.startExecution(
        prepared.run.id,
        scaffold.destination,
        architecture?.approval.id,
      )
      setExecution(result)
      setNotice(
        `Execution ${result.status}. Dependency bootstrap and independent verification now run in the sandbox.`,
      )
      setEvents(await v2Api.events(prepared.run.id))
    })
  }

  const clearSession = () => {
    localStorage.removeItem(SESSION_KEY)
    setProject(null)
    setSpec(null)
    setArchitecture(null)
    setPrepared(null)
    setScaffold(null)
    setExecution(null)
    setEvents([])
    setNotice('')
    setError('')
  }

  const stage = prepared
    ? 4
    : architecture
      ? 3
      : spec
        ? 2
        : project
          ? 1
          : 0

  return (
    <main className="v2-shell">
      <header className="v2-header">
        <div className="v2-brand">
          <div className="brand-mark" />
          <div>
            <div className="v2-wordmark">RI<span>CH</span></div>
            <div className="v2-submark">software development compiler</div>
          </div>
        </div>
        <div className="v2-health">
          <span className={`dot ${health ? '' : 'pulse'}`} />
          <div>
            <b>{health ? 'Control plane online' : 'Control plane offline'}</b>
            <span>
              {health
                ? `${health.api_version} · store schema ${health.store_schema_version}`
                : 'Start the local Canvas API'}
            </span>
          </div>
          <button className="tiny ghost" onClick={checkHealth}>Retry</button>
        </div>
      </header>

      <aside className="v2-rail">
        <div className="v2-rail-label">Compilation</div>
        {[
          ['01', 'Intent', 'Product truth'],
          ['02', 'Specification', 'Behavioral oracle'],
          ['03', 'Architecture', 'Owned boundaries'],
          ['04', 'Build run', 'Durable execution'],
          ['05', 'Evidence', 'Scaffold + events'],
        ].map(([number, label, detail], index) => (
          <div
            className={`v2-stage ${stage === index ? 'active' : ''} ${stage > index ? 'done' : ''}`}
            key={number}
          >
            <span>{stage > index ? '✓' : number}</span>
            <div><b>{label}</b><small>{detail}</small></div>
          </div>
        ))}
        <div className="v2-rail-principle">
          <span>Principle</span>
          <p>Intent and code are coequal artifacts. Neither advances without traceable evidence.</p>
        </div>
      </aside>

      <div className="v2-workspace">
        {connectionError && (
          <div className="v2-banner bad">
            <b>API unavailable</b>
            <span>{connectionError}</span>
          </div>
        )}
        {error && (
          <div className="v2-banner bad" role="alert">
            <b>Action stopped</b>
            <span>{error}</span>
            <button className="tiny ghost" onClick={() => setError('')}>Dismiss</button>
          </div>
        )}
        {notice && (
          <div className="v2-banner ok" role="status">
            <b>State updated</b>
            <span>{notice}</span>
            <button className="tiny ghost" onClick={() => setNotice('')}>Dismiss</button>
          </div>
        )}

        <section className="v2-hero">
          <span className="v2-eyebrow">RICH v2 · local-first control plane</span>
          <h1>Compile intent into<br /><em>evidence-backed software.</em></h1>
          <p>
            Define observable outcomes, approve the behavioral contract, inspect the
            architecture, then create a durable build run. Every transition is explicit.
          </p>
        </section>

        <section className="v2-panel">
          <div className="v2-section-title">
            <div>
              <span className="v2-eyebrow">Workspace</span>
              <h2>Create or select a project</h2>
            </div>
            {project && (
              <div className="v2-project-state">
                <span className="chip ok">selected</span>
                <code>rev {project.current_revision}</code>
              </div>
            )}
          </div>
          <div className="v2-project-form">
            <label>
              <span>Stable project id</span>
              <input
                className="mono"
                value={projectId}
                onChange={(event) => setProjectId(event.target.value)}
                placeholder="project.example"
              />
            </label>
            <label>
              <span>Project name</span>
              <input
                value={projectName}
                onChange={(event) => setProjectName(event.target.value)}
                placeholder="Example"
              />
            </label>
            <div className="v2-project-buttons">
              <button
                className="primary"
                disabled={!!busy || !projectId.trim() || !projectName.trim()}
                onClick={createProject}
              >
                {busy === 'create-project' ? 'Creating…' : 'Create'}
              </button>
              <button
                disabled={!!busy || !projectId.trim()}
                onClick={loadProject}
              >
                {busy === 'load-project' ? 'Loading…' : 'Load existing'}
              </button>
              {project && <button className="ghost danger" onClick={clearSession}>Clear session</button>}
            </div>
          </div>
          {project && (
            <div className="v2-object-bar">
              <div><span>Project</span><code title={project.id}>{project.id}</code></div>
              <div><span>Revision</span><b>{project.current_revision}</b></div>
              <div><span>Updated</span><b>{new Date(project.updated_at).toLocaleString()}</b></div>
            </div>
          )}
        </section>

        {project && (
          <section className="v2-panel">
            <div className="v2-section-title">
              <div>
                <span className="v2-eyebrow">Intent · revision {project.current_revision + 1}</span>
                <h2>Define the product truth</h2>
              </div>
              <span className="chip">draft</span>
            </div>
            <p className="v2-panel-lead">
              Requirements describe observable behavior. Scenarios define the evidence that
              makes each requirement provable.
            </p>
            <div className="v2-intent-form">
              <label className="v2-span-2">
                <span>Outcome and problem</span>
                <textarea
                  value={draft.goal}
                  onChange={(event) => setDraft({ ...draft, goal: event.target.value })}
                />
              </label>
              <label className="v2-span-2">
                <span>Audiences · one per line</span>
                <textarea
                  value={draft.audiences}
                  onChange={(event) => setDraft({ ...draft, audiences: event.target.value })}
                />
              </label>
            </div>
            <RequirementEditor
              title="Capabilities"
              note="The observable first-release product surface."
              items={draft.capabilities}
              onChange={(capabilities) => setDraft({ ...draft, capabilities })}
            />
            <RequirementEditor
              title="Quality constraints"
              note="Accessibility, security, performance, resilience, and device promises."
              items={draft.qualityConstraints}
              onChange={(qualityConstraints) => setDraft({ ...draft, qualityConstraints })}
            />
            <ScenarioEditor
              items={draft.scenarios}
              onChange={(scenarios) => setDraft({ ...draft, scenarios })}
            />
            <details className="v2-adaptive">
              <summary>Adaptive policies for identity, data, integrations, or realtime work</summary>
              <p>
                Fill the relevant policy if the goal or capabilities mention these concerns.
                The compiler fails closed when a relevant policy is missing.
              </p>
              <div className="v2-adaptive-grid">
                {[
                  ['Roles and permissions', 'roles', draft.roles],
                  ['Data lifecycle', 'dataPolicy', draft.dataPolicy],
                  ['Integration failure behavior', 'integrationFailurePolicy', draft.integrationFailurePolicy],
                  ['Concurrency and reconnects', 'concurrencyPolicy', draft.concurrencyPolicy],
                ].map(([label, key, value]) => (
                  <label key={key}>
                    <span>{label} · one rule per line</span>
                    <textarea
                      value={value}
                      onChange={(event) =>
                        setDraft({ ...draft, [key]: event.target.value })
                      }
                    />
                  </label>
                ))}
              </div>
            </details>
            <div className="v2-submit-row">
              <div>
                <b>{draft.capabilities.length + draft.qualityConstraints.length} requirements</b>
                <span>{draft.scenarios.length} acceptance scenarios</span>
              </div>
              <button className="primary" disabled={!!busy} onClick={submitSpec}>
                {busy === 'submit-spec' ? 'Compiling intent…' : 'Compile product specification →'}
              </button>
            </div>
          </section>
        )}

        {spec && (
          <>
            <section className="v2-panel">
              <div className="v2-section-title">
                <div>
                  <span className="v2-eyebrow">Versioned artifact</span>
                  <h2>{spec.spec.name} · product specification</h2>
                </div>
                <code>{shortId(spec.revision.id)}</code>
              </div>
              <div className="v2-spec-summary">
                <div><span>Requirements</span><b>{spec.spec.requirements.length}</b></div>
                <div><span>Scenarios</span><b>{spec.spec.acceptance_scenarios.length}</b></div>
                <div><span>Coverage</span><b className="v2-green">100%</b></div>
                <div><span>Schema</span><b>{spec.spec.schema_version}</b></div>
              </div>
              <ScenarioList scenarios={spec.spec.acceptance_scenarios} />
            </section>
            <ApprovalGate
              title="Approve the product specification"
              description="Approval freezes this revision as the behavioral contract used to plan the architecture. Later edits create a new revision."
              approval={spec.approval}
              actor={actor}
              busy={!!busy}
              onDecision={decideSpec}
            />
            {spec.approval.status === 'approved' && !architecture && (
              <section className="v2-panel v2-next-step">
                <div>
                  <span className="v2-eyebrow">Compiler stage</span>
                  <h2>Plan owned architecture boundaries</h2>
                  <p>The Next.js target pack will derive nodes, typed ports, dependencies, and requirement traces.</p>
                </div>
                <div className="v2-draft-actions">
                  <button className="primary" disabled={!!busy} onClick={() => draftArchitecture()}>
                    {busy === 'draft-architecture' ? 'Designing…' : 'Draft with the architect →'}
                  </button>
                  <button disabled={!!busy} onClick={proposeArchitecture}>
                    {busy === 'propose-architecture' ? 'Planning…' : 'Use the deterministic plan'}
                  </button>
                </div>
              </section>
            )}
            {architectureDraft && !architecture && (
              <ArchitectureDraftReview
                draft={architectureDraft}
                current={null}
                busy={!!busy}
                onApply={applyDraft}
                onRedraft={draftArchitecture}
                onDiscard={() => setArchitectureDraft(null)}
              />
            )}
          </>
        )}

        {architecture && (
          <>
            <section className="v2-panel">
              <div className="v2-section-title">
                <div>
                  <span className="v2-eyebrow">Architecture · {architecture.architecture.target_pack}</span>
                  <h2>Ownership and dependency graph</h2>
                </div>
                <span className="chip">{architecture.architecture.nodes.length} nodes</span>
              </div>
              <div className="v2-architecture-grid">
                {architecture.architecture.nodes.map((node) => (
                  <article className="v2-node-card" key={node.id}>
                    <div><code>{node.id}</code><span>{node.kind}</span></div>
                    <p>{node.name}</p>
                    <small>{node.owned_paths?.join(' · ') || 'No owned paths'}</small>
                  </article>
                ))}
              </div>
              <div className="v2-decision-grid">
                <div>
                  <h4>Decisions</h4>
                  <ul>{architecture.decisions.map((item) => <li key={item}>{item}</li>)}</ul>
                </div>
                <div>
                  <h4>Risks</h4>
                  <ul>{architecture.risks.map((item) => <li key={item}>{item}</li>)}</ul>
                </div>
              </div>
            </section>
            {architectureDraft && (
              <ArchitectureDraftReview
                draft={architectureDraft}
                current={architecture.architecture}
                busy={!!busy}
                onApply={applyDraft}
                onRedraft={draftArchitecture}
                onDiscard={() => setArchitectureDraft(null)}
              />
            )}
            {!architectureDraft && architecture.approval.status !== 'approved' && (
              <section className="v2-panel v2-next-step">
                <div>
                  <span className="v2-eyebrow">Not what you wanted?</span>
                  <h2>Ask for a different decomposition</h2>
                  <p>Rejecting alone leaves nothing to build. A new draft can be reviewed and applied as the next revision.</p>
                </div>
                <button disabled={!!busy} onClick={() => draftArchitecture()}>
                  {busy === 'draft-architecture' ? 'Designing…' : 'Draft an alternative →'}
                </button>
              </section>
            )}
            <ApprovalGate
              title="Approve the architecture"
              description="Approval authorizes compilation into durable tasks. It does not authorize deployment or other external side effects."
              approval={architecture.approval}
              actor={actor}
              busy={!!busy}
              onDecision={decideArchitecture}
            />
            {architecture.approval.status === 'approved' && !prepared && (
              <section className="v2-panel">
                <div className="v2-section-title">
                  <div>
                    <span className="v2-eyebrow">Budget boundary</span>
                    <h2>Prepare a durable run</h2>
                  </div>
                </div>
                <div className="v2-budget-row">
                  <label><span>Maximum model attempts</span><input type="number" min="1" value={maxAttempts} onChange={(event) => setMaxAttempts(event.target.value)} /></label>
                  <label><span>Maximum cost · USD</span><input value={maxCost} onChange={(event) => setMaxCost(event.target.value)} /></label>
                  <button className="primary" disabled={!!busy} onClick={prepareRun}>
                    {busy === 'prepare-run' ? 'Compiling…' : 'Compile durable run →'}
                  </button>
                </div>
              </section>
            )}
          </>
        )}

        {prepared && (
          <section className="v2-panel">
            <div className="v2-section-title">
              <div>
                <span className="v2-eyebrow">Run · {shortId(prepared.run.id)}</span>
                <h2>Compiled build plan</h2>
              </div>
              <span className={`chip ${statusClass(prepared.run.status)}`}>{prepared.run.status}</span>
            </div>
            <div className="v2-plan">
              {prepared.compiled.tasks.map((task, index) => (
                <div className="v2-plan-task" key={task.task_id}>
                  <span>{String(index + 1).padStart(2, '0')}</span>
                  <div><code>{task.node_id}</code><small>{task.owned_paths.join(' · ')}</small></div>
                  <b>{task.dependency_ids.length ? `${task.dependency_ids.length} deps` : 'root'}</b>
                </div>
              ))}
            </div>
            <div className="v2-digest">
              <span>Immutable plan artifact</span>
              <code>{prepared.plan_artifact_digest}</code>
            </div>
            <div className="v2-scaffold-form">
              <label>
                <span>Workspace-relative destination · must be absent or empty</span>
                <input className="mono" value={destination} onChange={(event) => setDestination(event.target.value)} />
              </label>
              <label>
                <span>Package scope</span>
                <input className="mono" value={packageScope} onChange={(event) => setPackageScope(event.target.value)} />
              </label>
              <button className="go" disabled={!!busy || !!scaffold} onClick={scaffoldRun}>
                {busy === 'scaffold' ? 'Writing scaffold…' : scaffold ? 'Scaffold created ✓' : 'Create Next.js scaffold'}
              </button>
            </div>
            {scaffold && (
              <div className="v2-execution-row">
                <div>
                  <b>Trusted execution</b>
                  <span>
                    Frozen install → lint → typecheck → unit → production build → acceptance
                  </span>
                </div>
                <button
                  className="primary"
                  disabled={
                    !!busy ||
                    execution?.active ||
                    prepared.run.status === 'succeeded' ||
                    ['failed', 'canceled'].includes(prepared.run.status)
                  }
                  onClick={executeRun}
                >
                  {busy === 'execute-run'
                    ? 'Starting…'
                    : execution?.active
                      ? 'Execution running…'
                      : prepared.run.status === 'succeeded'
                        ? 'Release verified ✓'
                        : ['running', 'verifying'].includes(prepared.run.status)
                          ? 'Resume durable execution'
                          : 'Execute verified build →'}
                </button>
              </div>
            )}
          </section>
        )}

        {prepared && (
          <section className="v2-panel">
            <div className="v2-section-title">
              <div>
                <span className="v2-eyebrow">Evidence stream</span>
                <h2>Durable run events</h2>
              </div>
              <span className="chip">{events.length} events</span>
            </div>
            {scaffold && (
              <div className="v2-scaffold-result">
                <div><span>Destination</span><code>{scaffold.destination}</code></div>
                <div><span>Content digest</span><code>{scaffold.manifest.content_digest}</code></div>
                <div><span>Manifest artifact</span><code>{scaffold.manifest_artifact_digest}</code></div>
              </div>
            )}
            <div className="v2-event-feed">
              {events.length === 0 && <p className="muted">Waiting for run evidence…</p>}
              {events.map((event) => (
                <article key={event.sequence}>
                  <span>{String(event.sequence).padStart(3, '0')}</span>
                  <div>
                    <b>{event.event_type}</b>
                    <small>{new Date(event.created_at).toLocaleTimeString()} · {event.task_id ? shortId(event.task_id) : 'run'}</small>
                  </div>
                  <code>{JSON.stringify(event.payload)}</code>
                </article>
              ))}
            </div>
          </section>
        )}

        {prepared && (
          <V2Inspector
            runId={prepared.run.id}
            events={events}
            projectId={project?.id}
          />
        )}

        <section className="v2-authority">
          <div>
            <span className="v2-eyebrow">Approval identity</span>
            <h3>Human decisions are durable evidence</h3>
          </div>
          <label><span>Actor</span><input value={actor} onChange={(event) => setActor(event.target.value)} /></label>
          <label><span>Decision reason</span><input value={reason} onChange={(event) => setReason(event.target.value)} /></label>
        </section>

        <footer className="v2-footer">
          <span>RICH v2 · local state, explicit authority, layered evidence</span>
          <code>{project?.id || 'no project selected'}</code>
        </footer>
      </div>
    </main>
  )
}
