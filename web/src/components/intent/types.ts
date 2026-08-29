/**
 * The interview's editable shape is the compiler's own: `InterviewAnswers`,
 * with ids the person never sees. One shape end to end -- what the interviewer
 * drafts, what the editor edits, what the server stores, what compiles.
 */

import type { InterviewAnswers } from '../../lib/api'

export type Answers = InterviewAnswers
export type RequirementItem = InterviewAnswers['capabilities'][number]
export type ScenarioItem = InterviewAnswers['scenarios'][number]

export const POLICY_KEYS = [
  'roles',
  'data_policy',
  'integration_failure_policy',
  'concurrency_policy',
] as const
export type PolicyKey = (typeof POLICY_KEYS)[number]

export const POLICY_LABELS: Record<PolicyKey, string> = {
  roles: 'Who can see and change what',
  data_policy: 'What is stored, for how long, and what is sensitive',
  integration_failure_policy: 'What people see when an outside service fails',
  concurrency_policy: 'What happens on simultaneous edits and reconnects',
}

export const emptyAnswers = (): Answers => ({
  goal: '',
  audiences: [],
  capabilities: [],
  quality_constraints: [],
  scenarios: [],
})

const cleanLines = (lines: string[] | undefined) =>
  (lines ?? []).map((line) => line.trim()).filter(Boolean)

/** What is sent: trimmed, empty lines dropped, empty policies omitted. */
export function trimAnswers(answers: Answers): Answers {
  const requirement = (item: RequirementItem): RequirementItem => ({
    id: item.id.trim(),
    title: item.title.trim(),
    statement: item.statement.trim(),
    ...(item.priority ? { priority: item.priority } : {}),
  })
  const result: Answers = {
    goal: answers.goal.trim(),
    audiences: cleanLines(answers.audiences),
    capabilities: answers.capabilities.map(requirement),
    quality_constraints: answers.quality_constraints.map(requirement),
    scenarios: answers.scenarios.map((scenario) => ({
      id: scenario.id.trim(),
      title: scenario.title.trim(),
      requirement_ids: scenario.requirement_ids,
      given: cleanLines(scenario.given),
      when: cleanLines(scenario.when),
      then: cleanLines(scenario.then),
      oracle: scenario.oracle,
    })),
  }
  for (const key of POLICY_KEYS) {
    const lines = cleanLines(answers[key])
    if (lines.length) result[key] = lines
  }
  return result
}

export const hasContent = (answers: Answers | null | undefined) =>
  !!answers &&
  (answers.goal.trim() !== '' ||
    answers.capabilities.length > 0 ||
    answers.scenarios.length > 0)

/** A small, complete example a first-time user can start from and edit. */
export const exampleAnswers = (): Answers => ({
  goal: 'Give technical founders a trustworthy way to turn approved product intent into a working web application.',
  audiences: ['Technical founders'],
  capabilities: [
    {
      id: 'req.workflow',
      title: 'Create a project workflow',
      statement: 'A founder can create a project and review each approval-gated build stage.',
    },
  ],
  quality_constraints: [
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
      requirement_ids: ['req.workflow'],
      given: ['A founder has opened the control plane.'],
      when: ['They create a project and submit its intent.'],
      then: ['The product specification is available for explicit approval.'],
      oracle: [
        { action: 'open_requirement' },
        { action: 'fill', locator: { kind: 'label', value: 'Project name' }, value: 'Example project' },
        { action: 'click', locator: { kind: 'role', value: 'button', name: 'Create project' } },
        { action: 'assert_visible', locator: { kind: 'text', value: 'approval', exact: false } },
      ],
    },
    {
      id: 'scenario.a11y',
      title: 'Complete the workflow with a keyboard',
      requirement_ids: ['req.a11y'],
      given: ['A founder uses only a keyboard.'],
      when: ['They move through the core project workflow.'],
      then: ['Every control can be reached, understood, and activated.'],
      oracle: [
        { action: 'open_requirement' },
        { action: 'keyboard', value: 'Tab' },
        { action: 'assert_focused', locator: { kind: 'label', value: 'Project name' } },
      ],
    },
  ],
})
