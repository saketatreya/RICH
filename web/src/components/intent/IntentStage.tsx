import type { Dispatch, SetStateAction } from 'react'

import type { AcceptanceVocabulary, InterviewDocument, Project } from '../../lib/api'
import ConversationPanel from './ConversationPanel'
import { RequirementEditor, ScenarioEditor } from './Editors'
import {
  type Answers,
  POLICY_KEYS,
  POLICY_LABELS,
  emptyAnswers,
  hasContent,
} from './types'

/**
 * The interview: what the product must do, in the architect's words.
 *
 * Two halves. On the left, a conversation: prose in, questions or a draft
 * back. On the right, the draft itself -- requirements and scenarios as
 * readable, editable rows, with every oracle step a sentence built from
 * dropdowns. Nothing here asks for an id or a line of JSON; what crosses the
 * boundary is the answers document and the actions that can be taken on it.
 */

export interface IntentStageProps {
  project: Project
  document: InterviewDocument
  vocabulary: AcceptanceVocabulary
  editAnswers: (update: (answers: Answers) => Answers) => void
  busy: string
  busySince: number
  setError: Dispatch<SetStateAction<string>>
  onTurn: (message: string) => void
  onSubmit: () => void
  onExample: () => void
}

export default function IntentStage({
  document,
  vocabulary,
  editAnswers,
  busy,
  busySince,
  onTurn,
  onSubmit,
  onExample,
}: IntentStageProps) {
  const answers = document.answers ?? emptyAnswers()
  const requirements = [...answers.capabilities, ...answers.quality_constraints]
  const taken = new Set(requirements.map((item) => item.id))
  const requirementCount = requirements.length
  const ready = requirementCount > 0 && answers.scenarios.length > 0 && answers.goal.trim() !== ''

  return (
    <section className="plane-panel" id="stage-intent">
      <div className="plane-section-title">
        <div>
          <span className="plane-eyebrow">Intent</span>
          <h2>Say what you want built</h2>
        </div>
        <span className="chip">draft</span>
      </div>
      <p className="plane-panel-lead">
        Describe it in prose on the left. The interviewer asks what it needs and
        drafts requirements and scenarios on the right — readable, editable, and
        yours to approve. Every scenario becomes a browser test the software must pass.
      </p>
      <div className="plane-interview">
        <ConversationPanel
          document={document}
          busy={busy}
          busySince={busySince}
          onSend={onTurn}
        />
        <div className="plane-draft">
          {!hasContent(document.answers) && (
            <div className="plane-draft-empty">
              <p>Nothing drafted yet. Say what you want on the left, or start from an example and edit it.</p>
              <button type="button" onClick={onExample}>Start from an example</button>
            </div>
          )}
          <div className="plane-intent-form">
            <label className="plane-span-2">
              <span>Outcome and problem</span>
              <textarea
                value={answers.goal}
                onChange={(event) =>
                  editAnswers((current) => ({ ...current, goal: event.target.value }))
                }
                placeholder="What should exist afterwards, and what problem does it replace?"
              />
            </label>
            <label className="plane-span-2">
              <span>Audiences · one per line</span>
              <textarea
                value={answers.audiences.join('\n')}
                onChange={(event) =>
                  editAnswers((current) => ({ ...current, audiences: event.target.value.split('\n') }))
                }
                placeholder="Who uses it, and who first"
              />
            </label>
          </div>
          <RequirementEditor
            title="Capabilities"
            note="What a person can do with the first release."
            items={answers.capabilities}
            taken={taken}
            onChange={(capabilities) => editAnswers((current) => ({ ...current, capabilities }))}
          />
          <RequirementEditor
            title="Quality constraints"
            note="Accessibility, security, performance, resilience, devices."
            items={answers.quality_constraints}
            taken={taken}
            onChange={(quality_constraints) =>
              editAnswers((current) => ({ ...current, quality_constraints }))
            }
          />
          <ScenarioEditor
            items={answers.scenarios}
            requirements={requirements.map((item) => ({ id: item.id, title: item.title }))}
            vocabulary={vocabulary}
            onChange={(scenarios) => editAnswers((current) => ({ ...current, scenarios }))}
          />
          <details className="plane-adaptive" open={POLICY_KEYS.some((key) => (answers[key] ?? []).length > 0)}>
            <summary>Policies the interviewer may ask about — roles, data, outside services, simultaneous edits</summary>
            <div className="plane-adaptive-grid">
              {POLICY_KEYS.map((key) => (
                <label key={key}>
                  <span>{POLICY_LABELS[key]} · one rule per line</span>
                  <textarea
                    value={(answers[key] ?? []).join('\n')}
                    onChange={(event) =>
                      editAnswers((current) => ({ ...current, [key]: event.target.value.split('\n') }))
                    }
                  />
                </label>
              ))}
            </div>
          </details>
          <div className="plane-submit-row">
            <div>
              <b>{requirementCount} requirements</b>
              <span>{answers.scenarios.length} scenarios</span>
            </div>
            <div className="plane-submit-actions">
              <button className="primary" disabled={!!busy || !ready} onClick={onSubmit}>
                {busy === 'submit-spec' ? 'Writing…' : 'Write the specification →'}
              </button>
            </div>
          </div>
        </div>
      </div>
    </section>
  )
}
