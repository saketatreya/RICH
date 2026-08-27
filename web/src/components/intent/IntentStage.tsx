import type { Dispatch, SetStateAction } from 'react'

import type { InterviewNeeds, InterviewAnswers, Project } from '../../lib/api'
import { api } from '../../lib/api'
import { RequirementEditor, ScenarioEditor } from './Editors'
import type { IntentDraft } from './types'

/**
 * The interview: what the product must do, in the architect's words.
 *
 * Held here rather than in the control plane because it is the one stage with
 * a substantial editing surface of its own -- capabilities, constraints,
 * scenarios and their browser oracles -- and none of that is anyone else's
 * business. What crosses the boundary is the draft, the actions that can be
 * taken on it, and whether something else is already running.
 */

export interface IntentStageProps {
  project: Project
  draft: IntentDraft
  setDraft: Dispatch<SetStateAction<IntentDraft>>
  answers: () => InterviewAnswers
  busy: string
  setBusy: Dispatch<SetStateAction<string>>
  setError: Dispatch<SetStateAction<string>>
  onSubmit: () => void
  needs: InterviewNeeds | null
  setNeeds: Dispatch<SetStateAction<InterviewNeeds | null>>
}

export default function IntentStage({
  project,
  draft,
  setDraft,
  answers,
  busy,
  setBusy,
  setError,
  onSubmit,
  needs: interviewNeeds,
  setNeeds: setInterviewNeeds,
}: IntentStageProps) {
  const submitSpec = onSubmit
  return (
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
  )
}
