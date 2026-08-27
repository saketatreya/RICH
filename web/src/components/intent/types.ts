/** The editable shapes the interview form works in, before they are
 * compiled into an approved specification. */

export type RequirementDraft = {
  id: string
  title: string
  statement: string
}

export type ScenarioDraft = {
  id: string
  title: string
  requirementIds: string
  given: string
  when: string
  then: string
  oracle: string
}

export type IntentDraft = {
  goal: string
  audiences: string
  capabilities: RequirementDraft[]
  qualityConstraints: RequirementDraft[]
  scenarios: ScenarioDraft[]
  roles: string
  dataPolicy: string
  integrationFailurePolicy: string
  concurrencyPolicy: string
}
