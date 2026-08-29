/** The interview's editable rows -- one requirement, one acceptance scenario --
 * and a read-only summary of what was compiled. No ids are typed here: they
 * are minted from titles and never shown. */

import type { AcceptanceScenario, AcceptanceVocabulary } from '../../lib/api'
import RequirementPicker from './RequirementPicker'
import ScenarioStepEditor from './ScenarioStepEditor'
import { describeStep, slugId } from './steps'
import type { RequirementItem, ScenarioItem } from './types'

const lines = (value: string[] | undefined) => (value ?? []).join('\n')
const split = (value: string) => value.split('\n')

export function RequirementEditor({
  title,
  note,
  items,
  taken,
  onChange,
}: {
  title: string
  note: string
  items: RequirementItem[]
  /** Every requirement id in use, so a new one never collides. */
  taken: Set<string>
  onChange: (items: RequirementItem[]) => void
}) {
  const update = (index: number, patch: Partial<RequirementItem>) =>
    onChange(items.map((item, i) => (i === index ? { ...item, ...patch } : item)))

  return (
    <div className="plane-form-section">
      <div className="plane-form-section-head">
        <div>
          <h4>{title}</h4>
          <p>{note}</p>
        </div>
        <button
          type="button"
          className="tiny ghost"
          onClick={() =>
            onChange([
              ...items,
              { id: slugId('req', `${title} ${items.length + 1}`, taken), title: '', statement: '' },
            ])
          }
        >
          + Add
        </button>
      </div>
      <div className="plane-editor-list">
        {items.map((item, index) => (
          <div className="plane-editor-card plane-requirement-card" key={item.id}>
            <input
              aria-label={`${title} ${index + 1} title`}
              value={item.title}
              onChange={(event) => update(index, { title: event.target.value })}
              placeholder="What a person can do"
            />
            <textarea
              aria-label={`${title} ${index + 1} statement`}
              value={item.statement}
              onChange={(event) => update(index, { statement: event.target.value })}
              placeholder="Describe it as something a person or a test can observe."
            />
            <button
              type="button"
              className="tiny ghost danger plane-remove"
              aria-label={`Remove ${title} ${index + 1}`}
              onClick={() => onChange(items.filter((_, i) => i !== index))}
            >
              Remove
            </button>
          </div>
        ))}
      </div>
    </div>
  )
}

export function ScenarioEditor({
  items,
  requirements,
  vocabulary,
  onChange,
}: {
  items: ScenarioItem[]
  requirements: Array<{ id: string; title: string }>
  vocabulary: AcceptanceVocabulary
  onChange: (items: ScenarioItem[]) => void
}) {
  const update = (index: number, patch: Partial<ScenarioItem>) =>
    onChange(items.map((item, i) => (i === index ? { ...item, ...patch } : item)))
  const taken = new Set(items.map((item) => item.id))

  return (
    <div className="plane-form-section">
      <div className="plane-form-section-head">
        <div>
          <h4>Scenarios</h4>
          <p>
            What a person does and what they then see. Each scenario becomes a
            browser test the generated software must pass.
          </p>
        </div>
        <button
          type="button"
          className="tiny ghost"
          onClick={() =>
            onChange([
              ...items,
              {
                id: slugId('scenario', `scenario ${items.length + 1}`, taken),
                title: '',
                requirement_ids: requirements.length === 1 ? [requirements[0].id] : [],
                given: [],
                when: [],
                then: [],
                oracle: [
                  { action: 'open_requirement' },
                  { action: 'assert_visible', locator: { kind: 'role', value: 'heading', name: '' } },
                ],
              },
            ])
          }
        >
          + Add
        </button>
      </div>
      <div className="plane-editor-list">
        {items.map((item, index) => (
          <div className="plane-editor-card plane-scenario-card" key={item.id}>
            <input
              aria-label={`Scenario ${index + 1} title`}
              value={item.title}
              onChange={(event) => update(index, { title: event.target.value })}
              placeholder="Scenario title"
            />
            <RequirementPicker
              label={`Scenario ${index + 1} proves`}
              options={requirements}
              selected={item.requirement_ids}
              onChange={(requirement_ids) => update(index, { requirement_ids })}
            />
            <div className="plane-gwt">
              <label>
                <span>Given</span>
                <textarea
                  value={lines(item.given)}
                  onChange={(event) => update(index, { given: split(event.target.value) })}
                  placeholder="One condition per line"
                />
              </label>
              <label>
                <span>When</span>
                <textarea
                  value={lines(item.when)}
                  onChange={(event) => update(index, { when: split(event.target.value) })}
                  placeholder="One action per line"
                />
              </label>
              <label>
                <span>Then</span>
                <textarea
                  value={lines(item.then)}
                  onChange={(event) => update(index, { then: split(event.target.value) })}
                  placeholder="One observable result per line"
                />
              </label>
            </div>
            <ScenarioStepEditor
              label={`Scenario ${index + 1} steps`}
              steps={item.oracle}
              vocabulary={vocabulary}
              onChange={(oracle) => update(index, { oracle })}
            />
            <button
              type="button"
              className="tiny ghost danger plane-remove"
              aria-label={`Remove scenario ${index + 1}`}
              onClick={() => onChange(items.filter((_, i) => i !== index))}
            >
              Remove
            </button>
          </div>
        ))}
      </div>
    </div>
  )
}

export function ScenarioList({ scenarios }: { scenarios: AcceptanceScenario[] }) {
  return (
    <div className="plane-evidence-grid">
      {scenarios.map((scenario) => (
        <article className="plane-evidence-card" key={scenario.id}>
          <h4>{scenario.title}</h4>
          <div className="plane-gwt-read">
            {!!scenario.given.length && <p><b>Given</b> {scenario.given.join(' · ')}</p>}
            <p><b>When</b> {scenario.when.join(' · ')}</p>
            <p><b>Then</b> {scenario.then.join(' · ')}</p>
          </div>
          <ol className="plane-steps-read">
            {scenario.oracle.map((step, index) => (
              <li key={index}>{describeStep(step)}</li>
            ))}
          </ol>
        </article>
      ))}
    </div>
  )
}
