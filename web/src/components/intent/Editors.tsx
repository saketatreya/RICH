/** The interview's editable rows: one requirement, one acceptance scenario,
 * and a read-only summary of what was compiled. */

import type { AcceptanceScenario } from '../../lib/api'
import type { RequirementDraft, ScenarioDraft } from './types'

export function RequirementEditor({
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

export function ScenarioEditor({
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

export function ScenarioList({ scenarios }: { scenarios: AcceptanceScenario[] }) {
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
