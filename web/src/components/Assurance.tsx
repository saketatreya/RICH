import { useEffect, useMemo, useState } from 'react'

import { type AcceptanceScenario, type Requirement, type RunEvent, api } from '../lib/api'

/** One step a failed acceptance run named, in the words the person approved. */
interface FailedStep {
  scenario_id: string
  step: string
  message: string
}

/**
 * What you asked for, and what proves it.
 *
 * This is the view the product is actually for. An architect decides what the
 * software must do and how it is shaped; whether the TypeScript that resulted
 * is any good is not their question, and reading it is not their job. Their
 * question is narrower and harder: *is this requirement true of the software,
 * and what established that?*
 *
 * Everything here comes from evidence the harness observed. A requirement is
 * proven because a gate the model cannot touch passed while carrying that
 * requirement's id — never because a model said it implemented it.
 */

type Level = 'proven' | 'partial' | 'failed' | 'pending'

interface Proof {
  requirement: Requirement
  level: Level
  gates: { kind: string; status: string; summary: string }[]
  scenarios: AcceptanceScenario[]
}

const LEVEL_LABEL: Record<Level, string> = {
  proven: 'proven',
  partial: 'partly proven',
  failed: 'failing',
  pending: 'not yet run',
}

// A blocking gate is one whose failure stops the run. Generation is not one of
// them, and must never be read as evidence that anything works.
const NOT_EVIDENCE = new Set(['generation'])

export default function Assurance({
  runId,
  requirements,
  scenarios,
  events,
}: {
  runId: string
  requirements: Requirement[]
  scenarios: AcceptanceScenario[]
  events: RunEvent[]
}) {
  // A failed acceptance gate names the step that failed, in the sentence the
  // person approved. That lives in the gate's result artifact; only failed
  // acceptance evidence is fetched, and only the latest attempt's is shown.
  const [failedSteps, setFailedSteps] = useState<Record<string, FailedStep[]>>({})
  useEffect(() => {
    const failed = events.filter(
      (event) =>
        event.event_type === 'evidence.recorded' &&
        event.payload.kind === 'acceptance' &&
        event.payload.status !== 'passed' &&
        typeof event.payload.result_digest === 'string',
    )
    const latest = failed[failed.length - 1]
    if (!latest) {
      setFailedSteps({})
      return
    }
    let cancelled = false
    api
      .artifact<{ failed_steps?: FailedStep[] }>(runId, String(latest.payload.result_digest))
      .then((artifact) => {
        if (cancelled) return
        const byScenario: Record<string, FailedStep[]> = {}
        for (const step of artifact.content?.failed_steps ?? []) {
          ;(byScenario[step.scenario_id] ??= []).push(step)
        }
        setFailedSteps(byScenario)
      })
      .catch(() => {
        // The evidence chips still say the gate failed; the step is a courtesy.
      })
    return () => {
      cancelled = true
    }
  }, [events, runId])

  const proofs = useMemo<Proof[]>(() => {
    const observed = events
      .filter((event) => event.event_type === 'evidence.recorded')
      .map((event) => ({
        kind: String(event.payload.kind ?? ''),
        status: String(event.payload.status ?? ''),
        summary: String(event.payload.summary ?? ''),
        requirementIds: Array.isArray(event.payload.requirement_ids)
          ? (event.payload.requirement_ids as string[])
          : [],
        scenarioIds: Array.isArray(event.payload.acceptance_scenario_ids)
          ? (event.payload.acceptance_scenario_ids as string[])
          : [],
      }))
      .filter((record) => !NOT_EVIDENCE.has(record.kind))

    return requirements.map((requirement) => {
      const gates = observed.filter((record) =>
        record.requirementIds.includes(requirement.id),
      )
      const covering = scenarios.filter((scenario) =>
        scenario.requirement_ids.includes(requirement.id),
      )
      const passedScenarios = new Set(
        gates.flatMap((record) =>
          record.status === 'passed' ? record.scenarioIds : [],
        ),
      )
      const failed = gates.some((record) => record.status !== 'passed')
      const passed = gates.filter((record) => record.status === 'passed')

      let level: Level = 'pending'
      if (failed) level = 'failed'
      else if (passed.length === 0) level = 'pending'
      else if (covering.length && !covering.every((s) => passedScenarios.has(s.id)))
        level = 'partial'
      else level = 'proven'

      return {
        requirement,
        level,
        gates: gates.map(({ kind, status, summary }) => ({ kind, status, summary })),
        scenarios: covering,
      }
    })
  }, [requirements, scenarios, events])

  const counted = proofs.reduce<Record<Level, number>>(
    (totals, proof) => ({ ...totals, [proof.level]: totals[proof.level] + 1 }),
    { proven: 0, partial: 0, failed: 0, pending: 0 },
  )

  return (
    <section className="plane-panel" id="stage-assurance">
      <div className="plane-section-title">
        <div>
          <span className="plane-eyebrow">Assurance</span>
          <h2>What you asked for, and what proves it</h2>
        </div>
        <span className={`chip ${counted.failed ? 'bad' : counted.proven === proofs.length && proofs.length ? 'ok' : 'warn'}`}>
          {counted.proven}/{proofs.length} proven
        </span>
      </div>

      <p className="muted">
        A requirement is proven because a gate the model cannot modify passed
        while carrying its id. Generation is excluded: that a worker wrote code
        is not evidence the code is right.
      </p>

      <div className="plane-assurance">
        {proofs.map((proof) => (
          <article key={proof.requirement.id} className={`plane-proof ${proof.level}`}>
            <header>
              <div>
                <b>{proof.requirement.title}</b>
                <small>{proof.requirement.statement}</small>
              </div>
              <span className={`chip ${proof.level === 'proven' ? 'ok' : proof.level === 'failed' ? 'bad' : 'warn'}`}>
                {LEVEL_LABEL[proof.level]}
              </span>
            </header>

            {proof.scenarios.length > 0 && (
              <ul className="plane-proof-scenarios">
                {proof.scenarios.map((scenario) => (
                  <li key={scenario.id}>
                    {scenario.title}
                    {(failedSteps[scenario.id] ?? []).map((failure, index) => (
                      <div className="plane-failed-step" key={index}>
                        <b>Failed at step</b> {failure.step}
                        {failure.message && <small>{failure.message}</small>}
                      </div>
                    ))}
                  </li>
                ))}
              </ul>
            )}

            {proof.gates.length > 0 ? (
              <div className="plane-proof-gates">
                {proof.gates.map((gate, index) => (
                  <span
                    key={`${gate.kind}-${index}`}
                    className={`chip ${gate.status === 'passed' ? 'ok' : 'bad'}`}
                    title={gate.summary}
                  >
                    {gate.kind}
                  </span>
                ))}
              </div>
            ) : (
              <p className="plane-proof-empty">
                Nothing has been observed about this requirement yet.
              </p>
            )}
          </article>
        ))}
        {proofs.length === 0 && (
          <p className="muted">No approved requirements yet.</p>
        )}
      </div>
    </section>
  )
}
