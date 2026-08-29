import { useEffect, useMemo, useState } from 'react'

import Behaviour, { type ContractDoc } from './Behaviour'
import CodeBlock, { langForPath } from './CodeBlock'
import {
  api,
  type ArchitectureNode,
  type DurableTask,
  type GeneratedSource,
  type RunArtifact,
  type RunEvent,
  type SourceJournal,
  type SourceTransaction,
} from '../lib/api'

/**
 * Read what the machine wrote, one node at a time.
 *
 * RICH could build software and then show a human nothing but an event feed —
 * approve or veto, with no way to see a line of the result. Everything here is
 * a read: the durable store is the source of truth and this only renders it.
 */

interface Props {
  runId: string
  events: RunEvent[]
  /** Set when the caller knows the project, which rebuilding a node needs. */
  projectId?: string
  /** The approved graph, so a node can be described by what it owns. */
  nodes?: ArchitectureNode[]
  /** The contracts, so a node can be described by what it promises. */
  contracts?: ContractDoc[]
  /** Selection shared with the architecture graph, so both read as one view. */
  selectedNode?: string | null
  onSelectNode?: (nodeId: string | null) => void
}

type DiffLine = { kind: ' ' | '-' | '+'; text: string }

/**
 * A minimal line diff. Deliberately not a dependency: the point is to show a
 * human what changed, and an LCS over the handful of files one task writes is
 * cheaper than another package in the supply chain.
 */
function diffLines(before: string, after: string): DiffLine[] {
  const a = before.length ? before.split('\n') : []
  const b = after.length ? after.split('\n') : []
  const lengths: number[][] = Array.from({ length: a.length + 1 }, () =>
    new Array(b.length + 1).fill(0),
  )
  for (let i = a.length - 1; i >= 0; i -= 1) {
    for (let j = b.length - 1; j >= 0; j -= 1) {
      lengths[i][j] =
        a[i] === b[j]
          ? lengths[i + 1][j + 1] + 1
          : Math.max(lengths[i + 1][j], lengths[i][j + 1])
    }
  }
  const out: DiffLine[] = []
  let i = 0
  let j = 0
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) {
      out.push({ kind: ' ', text: a[i] })
      i += 1
      j += 1
    } else if (lengths[i + 1][j] >= lengths[i][j + 1]) {
      out.push({ kind: '-', text: a[i] })
      i += 1
    } else {
      out.push({ kind: '+', text: b[j] })
      j += 1
    }
  }
  while (i < a.length) out.push({ kind: '-', text: a[i++] })
  while (j < b.length) out.push({ kind: '+', text: b[j++] })
  return out
}

function decodeBase64(value: string): string {
  try {
    const binary = atob(value)
    const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0))
    return new TextDecoder().decode(bytes)
  } catch {
    return ''
  }
}

function evidenceFor(events: RunEvent[], taskId: string) {
  return events
    .filter(
      (event) => event.event_type === 'evidence.recorded' && event.task_id === taskId,
    )
    .map((event) => ({
      sequence: event.sequence,
      kind: String(event.payload.kind ?? 'unknown'),
      status: String(event.payload.status ?? 'unknown'),
      summary: String(event.payload.summary ?? ''),
    }))
}

export default function Inspector({
  runId,
  events,
  projectId,
  nodes = [],
  contracts = [],
  selectedNode = null,
  onSelectNode,
}: Props) {
  const [tasks, setTasks] = useState<DurableTask[]>([])
  const [artifacts, setArtifacts] = useState<RunArtifact[]>([])
  const [transactions, setTransactions] = useState<SourceTransaction[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [source, setSource] = useState<GeneratedSource | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [rebuilding, setRebuilding] = useState(false)
  const [rebuilt, setRebuilt] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      try {
        const [nextTasks, nextArtifacts, nextTransactions] = await Promise.all([
          api.tasks(runId),
          api.artifacts(runId),
          api.sourceTransactions(runId),
        ])
        if (cancelled) return
        setTasks(nextTasks)
        setArtifacts(nextArtifacts)
        setTransactions(nextTransactions)
        setError(null)
      } catch (cause) {
        if (!cancelled) setError(cause instanceof Error ? cause.message : String(cause))
      }
    }
    void load()
    return () => {
      cancelled = true
    }
    // Re-read whenever the run produces something new.
  }, [runId, events.length])

  // The graph selects by node, this panel by durable task; either can drive
  // the other, and a click in one has to land in the other or they read as two
  // unrelated views of the same run.
  const selectedTask = useMemo(
    () =>
      tasks.find((task) => task.id === selected) ??
      tasks.find((task) => task.node_id === selectedNode) ??
      null,
    [tasks, selected, selectedNode],
  )

  const ownedRequirements = useMemo(
    () =>
      nodes.find((node) => node.id === selectedTask?.node_id)?.requirement_ids ??
      [],
    [nodes, selectedTask],
  )

  const contract = useMemo(() => {
    const node = nodes.find((item) => item.id === selectedTask?.node_id)
    if (!node?.contract_id) return null
    return contracts.find((item) => item.id === node.contract_id) ?? null
  }, [nodes, contracts, selectedTask])

  const sourceArtifact = useMemo(
    () =>
      artifacts.find(
        (artifact) =>
          artifact.task_id === selected && artifact.role === 'generated-source',
      ) ?? null,
    [artifacts, selected],
  )

  useEffect(() => {
    let cancelled = false
    if (!sourceArtifact) {
      setSource(null)
      return
    }
    setLoading(true)
    void api
      .artifact<GeneratedSource>(runId, sourceArtifact.digest)
      .then((result) => {
        if (!cancelled) setSource(result.content ?? null)
      })
      .catch((cause) => {
        if (!cancelled) setError(cause instanceof Error ? cause.message : String(cause))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [runId, sourceArtifact])

  // The transaction record carries digests, not bytes: the journal artifact is
  // where each file's pre-write content lives, and it is the only thing that
  // turns "here is the new file" into a real diff.
  const [originals, setOriginals] = useState<Map<string, string>>(new Map())

  useEffect(() => {
    let cancelled = false
    const journals = transactions.filter(
      (transaction) => transaction.task_id === selected,
    )
    if (!selected || journals.length === 0) {
      setOriginals(new Map())
      return
    }
    void Promise.all(
      journals.map((transaction) =>
        api
          .artifact<SourceJournal>(runId, transaction.journal_digest)
          .then((result) => result.content ?? null)
          .catch(() => null),
      ),
    ).then((results) => {
      if (cancelled) return
      const byPath = new Map<string, string>()
      for (const journal of results) {
        for (const file of journal?.files ?? []) {
          if (!file.original?.existed || !file.original.content_base64) continue
          byPath.set(file.path, decodeBase64(file.original.content_base64))
        }
      }
      setOriginals(byPath)
    })
    return () => {
      cancelled = true
    }
  }, [runId, transactions, selected])

  if (error) {
    return (
      <section className="plane-panel" id="stage-inspector">
        <div className="plane-section-title">
          <div>
            <span className="plane-eyebrow">Inspector</span>
            <h2>Generated source</h2>
          </div>
        </div>
        <p className="muted">Could not read run artifacts: {error}</p>
      </section>
    )
  }

  return (
    <section className="plane-panel">
      <div className="plane-section-title">
        <div>
          <span className="plane-eyebrow">Inspector</span>
          <h2>What each node produced</h2>
        </div>
        <span className="chip">{tasks.length} nodes</span>
      </div>

      <div className="plane-node-grid">
        {tasks.map((task) => (
          <button
            key={task.id}
            className={`plane-node-card${selectedTask?.id === task.id ? ' selected' : ''}`}
            onClick={() => {
              const next = selectedTask?.id === task.id ? null : task.id
              setSelected(next)
              onSelectNode?.(next === null ? null : task.node_id)
            }}
          >
            <b>{task.node_id}</b>
            <span className={`chip ${task.status}`}>{task.status}</span>
            <small>
              attempt {task.attempt} ·{' '}
              {artifacts.filter((artifact) => artifact.task_id === task.id).length} artifacts
            </small>
          </button>
        ))}
        {tasks.length === 0 && <p className="muted">No durable tasks yet.</p>}
      </div>

      {selectedTask && (
        <div className="plane-inspect">
          <div className="plane-inspect-head">
            <h3>{selectedTask.node_id}</h3>
            {projectId && (
              <button
                className="plane-secondary"
                disabled={rebuilding}
                onClick={async () => {
                  setRebuilding(true)
                  setRebuilt(null)
                  setError(null)
                  try {
                    const result = await api.rebuildNode(
                      projectId,
                      selectedTask.node_id,
                    )
                    setRebuilt(
                      result.forgotten_generations === 0
                        ? 'Nothing was remembered for this node; the next run writes it fresh.'
                        : `Forgot ${result.forgotten_generations} remembered generation(s). The next run rewrites this node and replays the rest.`,
                    )
                  } catch (cause) {
                    setError(
                      cause instanceof Error ? cause.message : String(cause),
                    )
                  } finally {
                    setRebuilding(false)
                  }
                }}
              >
                {rebuilding ? 'Marking…' : 'Rebuild this node'}
              </button>
            )}
          </div>
          {rebuilt && <p className="plane-note">{rebuilt}</p>}

          <h4>Responsible for</h4>
          <div className="plane-owns">
            {ownedRequirements.length ? (
              ownedRequirements.map((id) => (
                <span className="chip" key={id}>{id}</span>
              ))
            ) : (
              <span className="muted">No requirements allocated to this node.</span>
            )}
          </div>

          {contract && <Behaviour contract={contract} />}

          <h4>Evidence</h4>
          <div className="plane-evidence">
            {evidenceFor(events, selectedTask.id).map((record) => (
              <div key={record.sequence}>
                <span className={`chip ${record.status}`}>{record.status}</span>
                <b>{record.kind}</b>
                <small>{record.summary}</small>
              </div>
            ))}
            {evidenceFor(events, selectedTask.id).length === 0 && (
              <p className="muted">
                No evidence recorded for this node yet. Generation is not evidence;
                only an observed command result is.
              </p>
            )}
          </div>

          <details className="plane-audit">
            <summary>
              <b>Read the source this node wrote</b>
              <small>
                For audit. Nothing here needs your approval — the gates already
                decided whether it holds.
              </small>
            </summary>
            {loading && <p className="muted">Reading artifact…</p>}
            {!loading && !source && (
              <p className="muted">This node produced no source artifact.</p>
            )}
            {source && (
            <>
              <p className="muted">{source.summary}</p>
              {source.files.map((file) => {
                const before = originals.get(file.path)
                return (
                  <details key={file.path} className="artifact">
                    <summary>
                      {file.path} <small>{file.size} bytes</small>
                    </summary>
                    {before === undefined ? (
                      <CodeBlock code={file.content} lang={langForPath(file.path)} />
                    ) : (
                      <pre className="codeblock diff">
                        {diffLines(before, file.content).map((line, index) => (
                          <span key={index} className={`d-${line.kind === '+' ? 'add' : line.kind === '-' ? 'del' : 'same'}`}>
                            {line.kind}
                            {line.text}
                            {'\n'}
                          </span>
                        ))}
                      </pre>
                    )}
                  </details>
                )
              })}
            </>
            )}
          </details>
        </div>
      )}
    </section>
  )
}
