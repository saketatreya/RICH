/**
 * Oracle steps as sentences.
 *
 * A step is data -- {action, locator, value} -- and the person approving a
 * scenario should never have to read it as data. `describeStep` renders each
 * step as the sentence a tester would say; the Python side renders the same
 * sentence for the Playwright step title, so the canvas and the test log agree
 * word for word on what was checked.
 */

import type { AcceptanceStep, AcceptanceVocabulary, BrowserLocator } from '../../lib/api'

export type Action = AcceptanceStep['action']
export type LocatorKind = BrowserLocator['kind']

/** The vocabulary until the server has answered; identical to the models' own. */
export const FALLBACK_VOCABULARY: AcceptanceVocabulary = {
  actions: [
    { action: 'open_requirement', takes: [] },
    { action: 'navigate', takes: ['value'] },
    { action: 'click', takes: ['locator'] },
    { action: 'fill', takes: ['locator', 'value'] },
    { action: 'press', takes: ['locator', 'value'] },
    { action: 'keyboard', takes: ['value'] },
    { action: 'reload', takes: [] },
    { action: 'assert_visible', takes: ['locator'] },
    { action: 'assert_focused', takes: ['locator'] },
    { action: 'assert_text', takes: ['locator', 'value'] },
    { action: 'assert_value', takes: ['locator', 'value'] },
    { action: 'assert_url', takes: ['value'] },
  ],
  locator_kinds: ['role', 'label', 'text', 'test_id', 'placeholder'],
  roles: ['button', 'link', 'textbox', 'heading', 'checkbox', 'combobox', 'list', 'listitem', 'dialog', 'alert', 'status', 'navigation', 'main', 'form', 'search', 'tab', 'table', 'row', 'cell', 'img'],
  path_actions: ['navigate', 'assert_url'],
}

/** What a person reads instead of the action's identifier. */
export const ACTION_LABELS: Record<Action, string> = {
  open_requirement: 'Open the page for this requirement',
  navigate: 'Open a path',
  click: 'Click',
  fill: 'Type into',
  press: 'Press a key in',
  keyboard: 'Press a key',
  reload: 'Reload the page',
  assert_visible: 'Expect to see',
  assert_focused: 'Expect focus on',
  assert_text: 'Expect text in',
  assert_value: 'Expect the value of',
  assert_url: 'Expect the path to be',
}

export const LOCATOR_KIND_LABELS: Record<LocatorKind, string> = {
  role: 'the element with role',
  label: 'the field labelled',
  text: 'the text',
  test_id: 'the element with test id',
  placeholder: 'the field with placeholder',
}

export const fieldsFor = (vocabulary: AcceptanceVocabulary, action: Action) =>
  vocabulary.actions.find((entry) => entry.action === action)?.takes ?? []

export const isAssertion = (action: Action) => action.startsWith('assert_')

const quote = (value: string | undefined) => `‘${value ?? ''}’`

export function describeLocator(locator: BrowserLocator | undefined): string {
  if (!locator) return ''
  if (locator.kind === 'role') {
    return locator.name
      ? `the ${locator.value} named ${quote(locator.name)}`
      : `the ${locator.value}`
  }
  return `${LOCATOR_KIND_LABELS[locator.kind]} ${quote(locator.value)}`
}

export function describeStep(step: AcceptanceStep): string {
  const where = describeLocator(step.locator)
  switch (step.action) {
    case 'open_requirement':
      return 'Open the page for this requirement'
    case 'navigate':
      return `Open ${quote(step.value)}`
    case 'click':
      return `Click ${where}`
    case 'fill':
      return `Type ${quote(step.value)} into ${where}`
    case 'press':
      return `Press ${quote(step.value)} in ${where}`
    case 'keyboard':
      return `Press ${quote(step.value)}`
    case 'reload':
      return 'Reload the page'
    case 'assert_visible':
      return `Expect to see ${where}`
    case 'assert_focused':
      return `Expect focus on ${where}`
    case 'assert_text':
      return `Expect ${where} to say ${quote(step.value)}`
    case 'assert_value':
      return `Expect ${where} to hold ${quote(step.value)}`
    case 'assert_url':
      return `Expect the path to be ${quote(step.value)}`
  }
}

/** A step of the given action with exactly the fields that action takes. */
export function defaultStep(vocabulary: AcceptanceVocabulary, action: Action): AcceptanceStep {
  const takes = fieldsFor(vocabulary, action)
  const step: AcceptanceStep = { action }
  if (takes.includes('locator')) {
    step.locator = action === 'click'
      ? { kind: 'role', value: 'button', name: '' }
      : { kind: 'label', value: '' }
  }
  if (takes.includes('value')) {
    step.value = vocabulary.path_actions.includes(action) ? '/' : ''
  }
  return step
}

/** Keep a step's fields consistent with its action after an edit. */
export function conformStep(vocabulary: AcceptanceVocabulary, step: AcceptanceStep): AcceptanceStep {
  const takes = fieldsFor(vocabulary, step.action)
  const next: AcceptanceStep = { action: step.action }
  if (takes.includes('locator')) {
    next.locator = step.locator ?? defaultStep(vocabulary, step.action).locator
  }
  if (takes.includes('value')) {
    next.value = step.value ?? defaultStep(vocabulary, step.action).value
  }
  return next
}

/** The reasons a step could still be refused, said before the server says them. */
export function stepProblems(vocabulary: AcceptanceVocabulary, step: AcceptanceStep): string[] {
  const problems: string[] = []
  const takes = fieldsFor(vocabulary, step.action)
  if (takes.includes('locator')) {
    if (!step.locator?.value) problems.push('needs something to find')
    if (step.locator?.kind === 'role' && !vocabulary.roles.includes(step.locator.value)) {
      problems.push(`‘${step.locator.value}’ is not a role`)
    }
  }
  if (takes.includes('value')) {
    if (!step.value) problems.push('needs a value')
    else if (vocabulary.path_actions.includes(step.action) && !/^\/(?!\/)/.test(step.value)) {
      problems.push('a path starts with one slash')
    }
  }
  return problems
}

/** A stable id from a title, for things a person adds by hand. */
export function slugId(prefix: 'req' | 'scenario', title: string, taken: Set<string>): string {
  const base = title.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40) || 'item'
  let candidate = `${prefix}.${base}`
  let n = 2
  while (taken.has(candidate)) candidate = `${prefix}.${base}-${n++}`
  return candidate
}
