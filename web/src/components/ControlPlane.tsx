import { useEffect, useMemo, useState } from 'react'
import {
  type AcceptanceVocabulary,
  type ArchitectureDraft,
  type ArchitectureSubmission,
  type ExecutionStatus,
  type Health,
  type InterviewAnswers,
  type InterviewDocument,
  type PreparedRun,
  type Project,
  type ProjectState,
  type RunEvent,
  type ScaffoldResult,
  type SpecSubmission,
  ApiError,
  api,
  type DurableTask,
} from '../lib/api'
import ApprovalGate from './ApprovalGate'
import Assurance from './Assurance'
import type { ContractDoc } from './Behaviour'
import ArchitectureDraftReview from './ArchitectureDraftReview'
import ArchitectureGraph from './ArchitectureGraph'
import PreviewPanel from './PreviewPanel'
import Inspector from './Inspector'
import Waiting from './Waiting'
import IntentStage from './intent/IntentStage'
import { ScenarioList } from './intent/Editors'
import { FALLBACK_VOCABULARY } from './intent/steps'
import { type Answers, emptyAnswers, exampleAnswers, trimAnswers } from './intent/types'
import { errorMessage, shortId, statusClass } from '../lib/format'

/**
 * What survives a reload on this browser: a pointer to the project and who is
 * deciding. Everything the project holds -- spec, architecture, runs, the
 * interview draft -- comes back from the server, so nothing here can go stale.
 */
type SavedSession = {
  projectId: string | null
  actor: string
  reason: string
}

const SESSION_KEY = 'rich.control-plane.session'
const DEFAULT_ACTOR = 'founder'
const DEFAULT_REASON = 'Reviewed against the product intent.'
const architectureDraftKey = (projectId: string) =>
  `rich.architecture-draft.${projectId}`

function restoreSession(): SavedSession {
  const fallback: SavedSession = {
    projectId: null,
    actor: DEFAULT_ACTOR,
    reason: DEFAULT_REASON,
  }
  try {
    const raw = localStorage.getItem(SESSION_KEY)
    if (!raw) return fallback
    const parsed = JSON.parse(raw)
    return {
      // Earlier sessions stored full copies of server objects; only the
      // pointer is kept from them.
      projectId:
        typeof parsed.projectId === 'string'
          ? parsed.projectId
          : typeof parsed.project?.id === 'string'
            ? parsed.project.id
            : null,
      actor:
        typeof parsed.actor === 'string' && parsed.actor.trim()
          ? parsed.actor
          : DEFAULT_ACTOR,
      reason: typeof parsed.reason === 'string' ? parsed.reason : DEFAULT_REASON,
    }
  } catch {
    return fallback
  }
}

const emptyDocument = (): InterviewDocument => ({ transcript: [], answers: null })

/** The interview as the server stored it: the conversation and the answers so far. */
function draftFromDocument(document: unknown): InterviewDocument {
  if (!document || typeof document !== 'object') return emptyDocument()
  const stored = document as InterviewDocument
  const answers = stored.answers
  return {
    ...stored,
    transcript: Array.isArray(stored.transcript) ? stored.transcript : [],
    answers:
      answers && typeof answers === 'object' && typeof answers.goal === 'string'
        ? { ...emptyAnswers(), ...answers }
        : null,
  }
}

function readArchitectureDraft(projectId: string): ArchitectureDraft | null {
  try {
    const raw = localStorage.getItem(architectureDraftKey(projectId))
    return raw ? (JSON.parse(raw) as ArchitectureDraft) : null
  } catch {
    return null
  }
}

export default function ControlPlane() {
  const restored = useMemo(restoreSession, [])
  const [health, setHealth] = useState<Health | null>(null)
  const [connectionError, setConnectionError] = useState('')
  const [project, setProject] = useState<Project | null>(null)
  const [spec, setSpec] = useState<SpecSubmission | null>(null)
  const [selectedNode, setSelectedNode] = useState<string | null>(null)
  const [projects, setProjects] = useState<Project[]>([])
  const [runTasks, setRunTasks] = useState<DurableTask[]>([])
  const [architecture, setArchitecture] = useState<ArchitectureSubmission | null>(null)
  const [architectureDraft, setArchitectureDraft] =
    useState<ArchitectureDraft | null>(null)
  const [prepared, setPrepared] = useState<PreparedRun | null>(null)
  const [scaffold, setScaffold] = useState<ScaffoldResult | null>(null)
  const [events, setEvents] = useState<RunEvent[]>([])
  const [execution, setExecution] = useState<ExecutionStatus | null>(null)
  const [projectId, setProjectId] = useState(restored.projectId || 'project.rich-demo')
  const [projectName, setProjectName] = useState('RICH Demo')
  const [draft, setDraft] = useState<InterviewDocument>(emptyDocument)
  const [vocabulary, setVocabulary] = useState<AcceptanceVocabulary>(FALLBACK_VOCABULARY)
  // The server's counter for the interview draft, and whether this tab has
  // edits the server has not seen yet.
  const [draftRevision, setDraftRevision] = useState(0)
  const [draftDirty, setDraftDirty] = useState(false)
  const [actor, setActor] = useState(restored.actor)
  const [reason, setReason] = useState(restored.reason)
  const [maxAttempts, setMaxAttempts] = useState('20')
  const [maxCost, setMaxCost] = useState('10.00')
  const [destination, setDestination] = useState(
    `rich-${(restored.projectId || 'demo').split('.').pop()}`,
  )
  const [packageScope, setPackageScope] = useState('@rich-app')
  const [busy, setBusy] = useState('')
  const [busySince, setBusySince] = useState(() => Date.now())
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    try {
      localStorage.setItem(
        SESSION_KEY,
        JSON.stringify({ projectId: project?.id ?? null, actor, reason }),
      )
    } catch {
      // A browser that refuses storage still works; it just starts fresh.
    }
  }, [project?.id, actor, reason])

  // An unapplied architecture proposal is the one object worth keeping that
  // the server has not recorded -- by design, since nothing is recorded until
  // a human applies it. It is remembered per project on this browser only.
  useEffect(() => {
    if (!project) return
    try {
      if (architectureDraft) {
        localStorage.setItem(
          architectureDraftKey(project.id),
          JSON.stringify(architectureDraft),
        )
      } else {
        localStorage.removeItem(architectureDraftKey(project.id))
      }
    } catch {
      // Best effort.
    }
  }, [architectureDraft, project?.id])

  const editAnswers = (update: (answers: Answers) => Answers) => {
    setDraft((current) => ({ ...current, answers: update(current.answers ?? emptyAnswers()) }))
    setDraftDirty(true)
  }

  // The interview draft is saved to the server shortly after each edit, so a
  // reload never loses a word. The revision this tab last saw travels with the
  // save; a stale one is a conflict, and the server's version wins visibly.
  useEffect(() => {
    if (!project || !draftDirty) return
    const targetProject = project.id
    const timer = window.setTimeout(async () => {
      try {
        const saved = await api.saveInterview(
          targetProject,
          draft as Record<string, unknown>,
          draftRevision,
        )
        setDraftRevision(saved.draft_revision)
        setDraftDirty(false)
      } catch (saveError) {
        if (saveError instanceof ApiError && saveError.status === 409) {
          const latest = await api.getInterview(targetProject).catch(() => null)
          if (latest) {
            setDraft(draftFromDocument(latest.document))
            setDraftRevision(latest.draft_revision)
            setDraftDirty(false)
            setNotice('The interview was changed elsewhere; showing the latest version.')
          }
        }
        // Any other failure leaves the draft local and dirty; the next edit
        // retries, and the connection banner owns the explanation.
      }
    }, 900)
    return () => window.clearTimeout(timer)
  }, [draft, draftDirty, draftRevision, project?.id])

  const runAction = async (label: string, action: () => Promise<void>) => {
    setBusy(label)
    // Recorded so a slow step can say how long it has been slow for. Some of
    // these are one bounded model call and take minutes.
    setBusySince(Date.now())
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
    api
      .listProjects()
      .then(setProjects)
      // The banner owns connection errors; an empty list is a fine first run.
      .catch(() => setProjects([]))
    // The vocabulary an oracle step may use, from the models that decide it;
    // the built-in copy is identical and only bridges the first paint.
    api.acceptanceVocabulary().then(setVocabulary).catch(() => {})
    if (restored.projectId) loadProject(restored.projectId)
    // eslint-disable-next-line react-hooks/exhaustive-deps
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

  // Everything a project holds, as the server has it. Presence is restored
  // exactly as it would have been received the first time, so a reload or a
  // switch lands on the truth rather than on wherever this browser last was.
  const restoreProject = (state: ProjectState) => {
    setProject(state.project)
    setProjectId(state.project.id)
    setProjectName(state.project.name)
    setSpec(state.spec)
    setArchitecture(state.architecture)
    setPrepared(state.prepared)
    setScaffold(state.scaffold)
    setExecution(null)
    setEvents([])
    setRunTasks([])
    setSelectedNode(null)
    setDestination(`rich-${state.project.id.split('.').pop()}`)
    setDraft(draftFromDocument(state.interview?.document))
    setDraftRevision(state.interview?.draft_revision ?? 0)
    setDraftDirty(false)
    setArchitectureDraft(readArchitectureDraft(state.project.id))
  }

  const createProject = () =>
    runAction('create-project', async () => {
      const result = await api.createProject(projectId.trim(), projectName.trim())
      restoreProject(await api.projectState(result.id))
      setNotice(`Created ${result.id}. Intent compilation is ready.`)
    })

  const loadProject = (id?: string) =>
    runAction('load-project', async () => {
      const state = await api.projectState((id ?? projectId).trim())
      restoreProject(state)
      setNotice(
        `Loaded ${state.project.id} at revision ${state.project.current_revision}.`,
      )
    })

  const answers = (): InterviewAnswers => trimAnswers(draft.answers ?? emptyAnswers())

  const sendTurn = (message: string) => {
    if (!project) return
    runAction('interview-turn', async () => {
      const result = await api.interviewTurn(project.id, message, draftRevision)
      setDraft(draftFromDocument(result.draft.document))
      setDraftRevision(result.draft.draft_revision)
      setDraftDirty(false)
      setNotice(
        result.outcome.status === 'complete'
          ? 'The interviewer drafted a specification. Read it on the right, edit anything, then compile.'
          : result.outcome.status === 'questions'
            ? 'The interviewer has questions. Answer them in the conversation.'
            : 'The interviewer drafted what it could; the draft needs your edits before it compiles.',
      )
    })
  }

  const startFromExample = () => editAnswers(() => exampleAnswers())

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
      setNotice('Intent compiled into a versioned spec. Human approval is now required.')
    })
  }

  const decideSpec = (approved: boolean) => {
    if (!spec) return
    runAction('spec-decision', async () => {
      const approval = await api.decideApproval(spec.approval.id, approved, actor, reason)
      setSpec({ ...spec, approval })
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
      setArchitecture({ ...architecture, approval })
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
    setArchitectureDraft(null)
    setPrepared(null)
    setScaffold(null)
    setExecution(null)
    setEvents([])
    setDraft(emptyDocument())
    setDraftRevision(0)
    setDraftDirty(false)
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
    <main className="plane-shell">
      <header className="plane-header">
        <div className="plane-brand">
          <div className="brand-mark" />
          <div>
            <div className="plane-wordmark">RI<span>CH</span></div>
            <div className="plane-submark">software development compiler</div>
          </div>
        </div>
        <div className="plane-identity" aria-label="Approval identity">
          <label>
            <span>Deciding as</span>
            <input value={actor} onChange={(event) => setActor(event.target.value)} />
          </label>
          <label>
            <span>Reason on record</span>
            <input value={reason} onChange={(event) => setReason(event.target.value)} />
          </label>
        </div>
        <div className="plane-health">
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

      <aside className="plane-rail">
        <div className="plane-rail-label">Compilation</div>
        {stages.map((item, index) => (
          <button
            type="button"
            className={`plane-stage ${item.state}`}
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
        <div className="plane-rail-principle">
          <span>Principle</span>
          <p>Intent and code are coequal artifacts. Neither advances without traceable evidence.</p>
        </div>
      </aside>

      <div className="plane-workspace">
        {connectionError && (
          <div className="plane-banner bad">
            <b>API unavailable</b>
            <span>{connectionError}</span>
          </div>
        )}
        {error && (
          <div className="plane-banner bad" role="alert">
            <b>Action stopped</b>
            <span>{error}</span>
            <button className="tiny ghost" onClick={() => setError('')}>Dismiss</button>
          </div>
        )}
        {notice && (
          <div className="plane-banner ok" role="status">
            <b>State updated</b>
            <span>{notice}</span>
            <button className="tiny ghost" onClick={() => setNotice('')}>Dismiss</button>
          </div>
        )}

        <section className="plane-hero">
          <span className="plane-eyebrow">RICH · local-first</span>
          <h1>Compile intent into<br /><em>evidence-backed software.</em></h1>
          <p>
            Define observable outcomes, approve the behavioral contract, inspect the
            architecture, then create a durable build run. Every transition is explicit.
          </p>
        </section>

        <section className="plane-panel" id="stage-project">
          <div className="plane-section-title">
            <div>
              <span className="plane-eyebrow">Workspace</span>
              <h2>Create or select a project</h2>
            </div>
            {project && (
              <div className="plane-project-state">
                <span className="chip ok">selected</span>
                <code>rev {project.current_revision}</code>
              </div>
            )}
          </div>
          {projects.length > 0 && (
            <div className="plane-project-list">
              {projects.map((item) => (
                <button
                  type="button"
                  key={item.id}
                  className={`plane-project-chip${project?.id === item.id ? ' selected' : ''}`}
                  disabled={!!busy}
                  onClick={() => {
                    setProjectId(item.id)
                    setProjectName(item.name)
                    loadProject(item.id)
                  }}
                >
                  <b>{item.name}</b>
                  <small>
                    <code>{item.id}</code> · rev {item.current_revision}
                  </small>
                </button>
              ))}
            </div>
          )}
          <div className="plane-project-form">
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
            <div className="plane-project-buttons">
              <button
                className="primary"
                disabled={!!busy || !projectId.trim() || !projectName.trim()}
                onClick={createProject}
              >
                {busy === 'create-project' ? 'Creating…' : 'Create'}
              </button>
              <button
                disabled={!!busy || !projectId.trim()}
                onClick={() => loadProject()}
              >
                {busy === 'load-project' ? 'Loading…' : 'Load existing'}
              </button>
              {project && <button className="ghost danger" onClick={clearSession}>Clear session</button>}
            </div>
          </div>
          {project && (
            <div className="plane-object-bar">
              <div><span>Project</span><code title={project.id}>{project.id}</code></div>
              <div><span>Revision</span><b>{project.current_revision}</b></div>
              <div><span>Updated</span><b>{new Date(project.updated_at).toLocaleString()}</b></div>
            </div>
          )}
        </section>

        {project && (
          <IntentStage
            project={project}
            document={draft}
            vocabulary={vocabulary}
            editAnswers={editAnswers}
            busy={busy}
            busySince={busySince}
            setError={setError}
            onTurn={sendTurn}
            onSubmit={submitSpec}
            onExample={startFromExample}
          />
        )}

        {spec && (
          <>
            <section className="plane-panel">
              <div className="plane-section-title">
                <div>
                  <span className="plane-eyebrow">Versioned artifact</span>
                  <h2>{spec.spec.name} · product specification</h2>
                </div>
                <code>{shortId(spec.revision.id)}</code>
              </div>
              <div className="plane-spec-summary">
                <div><span>Requirements</span><b>{spec.spec.requirements.length}</b></div>
                <div><span>Scenarios</span><b>{spec.spec.acceptance_scenarios.length}</b></div>
                <div><span>Coverage</span><b className="plane-green">100%</b></div>
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
              <section className="plane-panel plane-next-step">
                <div>
                  <span className="plane-eyebrow">Compiler stage</span>
                  <h2>Plan owned architecture boundaries</h2>
                  <p>The Next.js target pack will derive nodes, typed ports, dependencies, and requirement traces.</p>
                  {busy === 'draft-architecture' && (
                    <Waiting
                      since={busySince}
                      what="The architect is designing the decomposition"
                      typical="one bounded model call, usually 1–3 minutes"
                    />
                  )}
                </div>
                <div className="plane-draft-actions">
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
            <section className="plane-panel" id="stage-architecture">
              <div className="plane-section-title">
                <div>
                  <span className="plane-eyebrow">Architecture · {architecture.architecture.target_pack}</span>
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
              <div className="plane-architecture-grid">
                {architecture.architecture.nodes.map((node) => (
                  <article
                    className={`plane-node-card${selectedNode === node.id ? ' selected' : ''}`}
                    key={node.id}
                  >
                    <div><code>{node.id}</code><span>{node.kind}</span></div>
                    <p>{node.name}</p>
                    <small>{node.owned_paths?.join(' · ') || 'No owned paths'}</small>
                  </article>
                ))}
              </div>
              <div className="plane-decision-grid">
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
              <section className="plane-panel plane-next-step">
                <div>
                  <span className="plane-eyebrow">Not what you wanted?</span>
                  <h2>Ask for a different decomposition</h2>
                  <p>Rejecting alone leaves nothing to build. A new draft can be reviewed and applied as the next revision.</p>
                  {busy === 'draft-architecture' && (
                    <Waiting
                      since={busySince}
                      what="The architect is designing an alternative"
                      typical="one bounded model call, usually 1–3 minutes"
                    />
                  )}
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
              <section className="plane-panel" id="stage-run">
                <div className="plane-section-title">
                  <div>
                    <span className="plane-eyebrow">Budget boundary</span>
                    <h2>Prepare a durable run</h2>
                  </div>
                </div>
                <div className="plane-budget-row">
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
          <section className="plane-panel">
            <div className="plane-section-title">
              <div>
                <span className="plane-eyebrow">Run · {shortId(prepared.run.id)}</span>
                <h2>Compiled build plan</h2>
              </div>
              <div className="plane-run-state">
                {prepared.run.status === 'succeeded' && (
                  <a
                    className="plane-download"
                    href={api.releaseUrl(prepared.run.id)}
                    download
                    title="The exact source the gates verified, as a ZIP bound to this run's evidence"
                  >
                    Download release ZIP
                  </a>
                )}
                <span className={`chip ${statusClass(prepared.run.status)}`}>{prepared.run.status}</span>
              </div>
            </div>
            <div className="plane-plan">
              {prepared.compiled.tasks.map((task, index) => (
                <div className="plane-plan-task" key={task.task_id}>
                  <span>{String(index + 1).padStart(2, '0')}</span>
                  <div><code>{task.node_id}</code><small>{task.owned_paths.join(' · ')}</small></div>
                  <b>{task.dependency_ids.length ? `${task.dependency_ids.length} deps` : 'root'}</b>
                </div>
              ))}
            </div>
            <div className="plane-digest">
              <span>Immutable plan artifact</span>
              <code>{prepared.plan_artifact_digest}</code>
            </div>
            <div className="plane-scaffold-form">
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
              <div className="plane-execution-row">
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
                    className="plane-secondary"
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
          <section className="plane-panel">
            <div className="plane-section-title">
              <div>
                <span className="plane-eyebrow">Evidence stream</span>
                <h2>Durable run events</h2>
              </div>
              <span className="chip">{events.length} events</span>
            </div>
            {scaffold && (
              <div className="plane-scaffold-result">
                <div><span>Destination</span><code>{scaffold.destination}</code></div>
                <div><span>Content digest</span><code>{scaffold.manifest.content_digest}</code></div>
                <div><span>Manifest artifact</span><code>{scaffold.manifest_artifact_digest}</code></div>
              </div>
            )}
            <div className="plane-event-feed">
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
            sourceDir={scaffold?.destination ?? null}
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
            contracts={
              architecture?.architecture.contracts as unknown as ContractDoc[]
            }
            selectedNode={selectedNode}
            onSelectNode={setSelectedNode}
          />
        )}

        <footer className="plane-footer">
          <span>RICH · local state, explicit authority, layered evidence</span>
          <code>{project?.id || 'no project selected'}</code>
        </footer>
      </div>
    </main>
  )
}
