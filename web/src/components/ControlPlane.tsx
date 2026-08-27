import { useEffect, useMemo, useState } from 'react'
import {
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
  api,
  type DurableTask,
  type InterviewNeeds,
} from '../lib/api'
import ApprovalGate from './ApprovalGate'
import Assurance from './Assurance'
import ArchitectureDraftReview from './ArchitectureDraftReview'
import ArchitectureGraph from './ArchitectureGraph'
import PreviewPanel from './PreviewPanel'
import Inspector from './Inspector'
import { RequirementEditor, ScenarioEditor, ScenarioList } from './intent/Editors'
import type { IntentDraft } from './intent/types'
import { commaList, errorMessage, lines, shortId, statusClass } from '../lib/format'

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

export default function ControlPlane() {
  const restored = useMemo(restoreSession, [])
  const [health, setHealth] = useState<Health | null>(null)
  const [connectionError, setConnectionError] = useState('')
  const [project, setProject] = useState<Project | null>(restored.project)
  const [spec, setSpec] = useState<SpecSubmission | null>(restored.spec)
  const [selectedNode, setSelectedNode] = useState<string | null>(null)
  const [interviewNeeds, setInterviewNeeds] = useState<InterviewNeeds | null>(null)
  const [runTasks, setRunTasks] = useState<DurableTask[]>([])
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
      const result = await api.health()
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
        const [nextEvents, nextRun, nextExecution, nextTasks] = await Promise.all([
          api.events(prepared.run.id),
          api.getRun(prepared.run.id),
          api.execution(prepared.run.id),
          // Fed to the architecture graph, so the shape a human approved is
          // the same picture that shows where the work currently is.
          api.tasks(prepared.run.id),
        ])
        if (!cancelled) {
          setEvents(nextEvents)
          setExecution(nextExecution)
          setRunTasks(nextTasks)
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
      const result = await api.createProject(projectId.trim(), projectName.trim())
      resetAfterProject(result)
      setNotice(`Created ${result.id}. Intent compilation is ready.`)
    })

  const loadProject = () =>
    runAction('load-project', async () => {
      const result = await api.getProject(projectId.trim())
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
      const result = await api.submitSpec(project, answers())
      const refreshed = await api.getProject(project.id)
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
      const approval = await api.decideApproval(spec.approval.id, approved, actor, reason)
      const next = { ...spec, approval }
      setSpec(next)
      persist({ spec: next })
      setNotice(approved ? 'Product specification approved.' : 'Product specification rejected.')
    })
  }

  const draftArchitecture = (repair?: string) => {
    if (!project || !spec) return
    runAction('draft-architecture', async () => {
      const result = await api.draftArchitecture(
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
      const result = await api.reviseArchitecture(
        project,
        spec.revision.id,
        spec.approval.id,
        architectureDraft.architecture,
        architectureDraft.decisions,
        architectureDraft.risks,
      )
      const refreshed = await api.getProject(project.id)
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
      const result = await api.proposeArchitecture(
        project,
        spec.revision.id,
        spec.approval.id,
      )
      const refreshed = await api.getProject(project.id)
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
      const approval = await api.decideApproval(
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
      const result = await api.prepareRun(architecture.approval.id, {
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
      const result = await api.scaffold(
        prepared.run.id,
        destination.trim(),
        packageScope.trim() || undefined,
      )
      setScaffold(result)
      persist({ scaffold: result })
      setNotice(`Scaffold written to ${result.destination}.`)
      setEvents(await api.events(prepared.run.id))
    })
  }

  const executeRun = () => {
    if (!prepared || !scaffold) return
    runAction('execute-run', async () => {
      const result = await api.startExecution(
        prepared.run.id,
        scaffold.destination,
        architecture?.approval.id,
      )
      setExecution(result)
      setNotice(
        `Execution ${result.status}. Dependency bootstrap and independent verification now run in the sandbox.`,
      )
      setEvents(await api.events(prepared.run.id))
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

  // Derived from the durable objects rather than a step counter, so a reload
  // or a resumed run lands on the truth instead of on wherever the session
  // last was.
  // Presence is not progress: a spec that exists but is unapproved is not a
  // finished stage, and the old index-based counter could not say so.
  const stages: Array<{
    id: string
    label: string
    detail: string
    state: 'done' | 'active' | 'blocked'
  }> = [
    {
      id: 'stage-project',
      label: 'Project',
      detail: project ? project.name : 'Name the workspace',
      state: project ? 'done' : 'active',
    },
    {
      id: 'stage-intent',
      label: 'Intent',
      detail:
        spec?.approval.status === 'approved'
          ? 'Specification approved'
          : spec
            ? 'Awaiting your approval'
            : 'Describe the outcomes',
      state:
        spec?.approval.status === 'approved'
          ? 'done'
          : project
            ? 'active'
            : 'blocked',
    },
    {
      id: 'stage-architecture',
      label: 'Architecture',
      detail:
        architecture?.approval.status === 'approved'
          ? 'Graph approved'
          : architecture
            ? 'Review, revise, approve'
            : 'Design the components',
      state:
        architecture?.approval.status === 'approved'
          ? 'done'
          : spec?.approval.status === 'approved'
            ? 'active'
            : 'blocked',
    },
    {
      id: 'stage-run',
      label: 'Run',
      detail: prepared
        ? `Run ${shortId(prepared.run.id)} · ${prepared.run.status}`
        : 'Set a budget and prepare',
      state: prepared
        ? prepared.run.status === 'succeeded'
          ? 'done'
          : 'active'
        : architecture?.approval.status === 'approved'
          ? 'active'
          : 'blocked',
    },
    {
      id: 'stage-assurance',
      label: 'Assurance',
      detail: prepared
        ? 'What is proven, and by what'
        : 'Needs a run',
      state: prepared ? 'active' : 'blocked',
    },
    {
      id: 'stage-preview',
      label: 'Preview',
      detail:
        prepared?.run.status === 'succeeded'
          ? 'Deploy what was verified'
          : 'Needs a verified run',
      state:
        prepared?.run.status === 'succeeded' ? 'active' : 'blocked',
    },
  ]

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
        {stages.map((item, index) => (
          <button
            type="button"
            className={`v2-stage ${item.state}`}
            key={item.id}
            aria-current={item.state === 'active' ? 'step' : undefined}
            onClick={() =>
              document
                .getElementById(item.id)
                ?.scrollIntoView({ behavior: 'smooth', block: 'start' })
            }
          >
            <span>{item.state === 'done' ? '✓' : `0${index + 1}`}</span>
            <div><b>{item.label}</b><small>{item.detail}</small></div>
          </button>
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
          <span className="v2-eyebrow">RICH · local-first</span>
          <h1>Compile intent into<br /><em>evidence-backed software.</em></h1>
          <p>
            Define observable outcomes, approve the behavioral contract, inspect the
            architecture, then create a durable build run. Every transition is explicit.
          </p>
        </section>

        <section className="v2-panel" id="stage-project">
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
          <section className="v2-panel" id="stage-intent">
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
            {interviewNeeds && (
              <div className="v2-needs">
                {interviewNeeds.complete ? (
                  <p className="v2-needs-done">
                    Nothing outstanding — every question this project raises has an answer.
                  </p>
                ) : (
                  <>
                    <b>Still needed for this project</b>
                    <ul>
                      {interviewNeeds.questions.map((question) => (
                        <li key={question.id}>
                          <span>{question.prompt}</span>
                          <small>{question.rationale}</small>
                        </li>
                      ))}
                    </ul>
                  </>
                )}
              </div>
            )}
            <div className="v2-submit-row">
              <div>
                <b>{draft.capabilities.length + draft.qualityConstraints.length} requirements</b>
                <span>{draft.scenarios.length} acceptance scenarios</span>
              </div>
              <div className="v2-submit-actions">
                <button
                  className="v2-secondary"
                  disabled={!!busy || !project}
                  onClick={async () => {
                    if (!project) return
                    setBusy('interview-needs')
                    try {
                      setInterviewNeeds(
                        await api.interviewNeeds(
                          project.id,
                          project.name,
                          answers(),
                        ),
                      )
                    } catch (cause) {
                      setError(
                        cause instanceof Error ? cause.message : String(cause),
                      )
                    } finally {
                      setBusy('')
                    }
                  }}
                >
                  {busy === 'interview-needs' ? 'Checking…' : 'What else do you need?'}
                </button>
                <button className="primary" disabled={!!busy} onClick={submitSpec}>
                  {busy === 'submit-spec' ? 'Compiling intent…' : 'Compile product specification →'}
                </button>
              </div>
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
            <section className="v2-panel" id="stage-architecture">
              <div className="v2-section-title">
                <div>
                  <span className="v2-eyebrow">Architecture · {architecture.architecture.target_pack}</span>
                  <h2>Ownership and dependency graph</h2>
                </div>
                <span className="chip">{architecture.architecture.nodes.length} nodes</span>
              </div>
              <ArchitectureGraph
                architecture={architecture.architecture}
                tasks={runTasks}
                selected={selectedNode}
                onSelect={(nodeId) =>
                  setSelectedNode(nodeId === selectedNode ? null : nodeId)
                }
              />
              <div className="v2-architecture-grid">
                {architecture.architecture.nodes.map((node) => (
                  <article
                    className={`v2-node-card${selectedNode === node.id ? ' selected' : ''}`}
                    key={node.id}
                  >
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
              <section className="v2-panel" id="stage-run">
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
                    Frozen install → lint → typecheck → unit → contract obligations →
                    production build → acceptance
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
                {execution?.active && (
                  <button
                    className="v2-secondary"
                    disabled={busy === 'cancel-run'}
                    onClick={async () => {
                      setBusy('cancel-run')
                      try {
                        await api.cancelRun(prepared.run.id)
                      } catch (cause) {
                        setError(
                          cause instanceof Error ? cause.message : String(cause),
                        )
                      } finally {
                        setBusy('')
                      }
                    }}
                  >
                    {busy === 'cancel-run' ? 'Asking…' : 'Stop at next checkpoint'}
                  </button>
                )}
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

        {prepared && spec && (
          <Assurance
            requirements={spec.spec.requirements}
            scenarios={spec.spec.acceptance_scenarios}
            events={events}
          />
        )}

        {prepared && (
          <div id="stage-preview">
          <PreviewPanel
            key="preview"
            run={prepared.run}
            destination={destination}
            actor={actor}
          />
          </div>
        )}

        {prepared && (
          <Inspector
            runId={prepared.run.id}
            events={events}
            projectId={project?.id}
            nodes={architecture?.architecture.nodes}
            selectedNode={selectedNode}
            onSelectNode={setSelectedNode}
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
