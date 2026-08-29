import type { AcceptanceStep, AcceptanceVocabulary, BrowserLocator } from '../../lib/api'
import {
  ACTION_LABELS,
  LOCATOR_KIND_LABELS,
  type Action,
  type LocatorKind,
  conformStep,
  defaultStep,
  describeStep,
  fieldsFor,
  stepProblems,
} from './steps'

/**
 * The oracle as sentence rows. Every control is a dropdown bound to the
 * vocabulary or a text field for the words a person chooses; the step data is
 * generated from them and never typed.
 */
export default function ScenarioStepEditor({
  label,
  steps,
  vocabulary,
  onChange,
}: {
  label: string
  steps: AcceptanceStep[]
  vocabulary: AcceptanceVocabulary
  onChange: (steps: AcceptanceStep[]) => void
}) {
  const replace = (index: number, next: AcceptanceStep) =>
    onChange(steps.map((step, i) => (i === index ? conformStep(vocabulary, next) : step)))

  const changeAction = (index: number, action: Action) => {
    const fresh = defaultStep(vocabulary, action)
    const previous = steps[index]
    // Keep what carries over: a locator when both actions take one, a value
    // when both take one and neither is a path.
    if (fresh.locator && previous.locator) fresh.locator = previous.locator
    if (
      fresh.value !== undefined &&
      previous.value !== undefined &&
      !vocabulary.path_actions.includes(action) &&
      !vocabulary.path_actions.includes(previous.action)
    ) {
      fresh.value = previous.value
    }
    replace(index, fresh)
  }

  const changeLocator = (index: number, locator: BrowserLocator) =>
    replace(index, { ...steps[index], locator })

  const move = (index: number, delta: number) => {
    const next = [...steps]
    const target = index + delta
    ;[next[index], next[target]] = [next[target], next[index]]
    onChange(next)
  }

  return (
    <div className="plane-steps" aria-label={label}>
      <ol>
        {steps.map((step, index) => {
          const takes = fieldsFor(vocabulary, step.action)
          const problems = stepProblems(vocabulary, step)
          const locator = step.locator
          return (
            <li className={`plane-step${problems.length ? ' invalid' : ''}`} key={index}>
              <div className="plane-step-sentence">
                <span className="plane-step-n">{index + 1}</span>
                {describeStep(step)}
                {problems.length > 0 && <small> · {problems.join(', ')}</small>}
              </div>
              <div className="plane-step-controls">
                <select
                  aria-label={`Step ${index + 1} action`}
                  value={step.action}
                  onChange={(event) => changeAction(index, event.target.value as Action)}
                >
                  {vocabulary.actions.map((entry) => (
                    <option key={entry.action} value={entry.action}>
                      {ACTION_LABELS[entry.action]}
                    </option>
                  ))}
                </select>
                {takes.includes('locator') && locator && (
                  <>
                    <select
                      aria-label={`Step ${index + 1} find by`}
                      value={locator.kind}
                      onChange={(event) => {
                        const kind = event.target.value as LocatorKind
                        changeLocator(
                          index,
                          kind === 'role'
                            ? { kind, value: 'button', name: '' }
                            : { kind, value: locator.kind === 'role' ? '' : locator.value },
                        )
                      }}
                    >
                      {vocabulary.locator_kinds.map((kind) => (
                        <option key={kind} value={kind}>
                          {LOCATOR_KIND_LABELS[kind]}
                        </option>
                      ))}
                    </select>
                    {locator.kind === 'role' ? (
                      <>
                        <select
                          aria-label={`Step ${index + 1} role`}
                          value={locator.value}
                          onChange={(event) =>
                            changeLocator(index, { ...locator, value: event.target.value })
                          }
                        >
                          {vocabulary.roles.map((role) => (
                            <option key={role} value={role}>
                              {role}
                            </option>
                          ))}
                        </select>
                        <input
                          aria-label={`Step ${index + 1} name`}
                          placeholder="named…"
                          value={locator.name ?? ''}
                          onChange={(event) =>
                            changeLocator(index, { ...locator, name: event.target.value })
                          }
                        />
                      </>
                    ) : (
                      <input
                        aria-label={`Step ${index + 1} target`}
                        placeholder={locator.kind === 'text' ? 'text on the page' : 'label text'}
                        value={locator.value}
                        onChange={(event) =>
                          changeLocator(index, { ...locator, value: event.target.value })
                        }
                      />
                    )}
                  </>
                )}
                {takes.includes('value') && (
                  <input
                    aria-label={`Step ${index + 1} value`}
                    placeholder={vocabulary.path_actions.includes(step.action) ? '/path' : 'value'}
                    value={step.value ?? ''}
                    onChange={(event) => replace(index, { ...step, value: event.target.value })}
                  />
                )}
                <span className="plane-step-tools">
                  <button
                    type="button"
                    className="tiny ghost"
                    aria-label={`Move step ${index + 1} up`}
                    disabled={index === 0}
                    onClick={() => move(index, -1)}
                  >
                    ↑
                  </button>
                  <button
                    type="button"
                    className="tiny ghost"
                    aria-label={`Move step ${index + 1} down`}
                    disabled={index === steps.length - 1}
                    onClick={() => move(index, 1)}
                  >
                    ↓
                  </button>
                  <button
                    type="button"
                    className="tiny ghost danger"
                    aria-label={`Remove step ${index + 1}`}
                    onClick={() => onChange(steps.filter((_, i) => i !== index))}
                  >
                    Remove
                  </button>
                </span>
              </div>
            </li>
          )
        })}
      </ol>
      <label className="plane-step-add">
        <span>Add a step</span>
        <select
          aria-label={`${label} · add a step`}
          value=""
          onChange={(event) => {
            const action = event.target.value as Action | ''
            if (action) onChange([...steps, defaultStep(vocabulary, action)])
          }}
        >
          <option value="">Choose what happens next…</option>
          {vocabulary.actions.map((entry) => (
            <option key={entry.action} value={entry.action}>
              {ACTION_LABELS[entry.action]}
            </option>
          ))}
        </select>
      </label>
    </div>
  )
}
