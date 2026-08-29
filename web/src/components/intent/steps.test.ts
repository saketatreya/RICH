// The canvas and the Playwright step titles must say the same words: a
// person who approved "Expect to see 'Buy milk'" reads exactly that in a
// failure. tests/fixtures/step_sentences.json is written by the Python side
// (describe_step) and holds both renderers to each other.
import { describe, expect, it } from 'vitest'

import fixture from '../../../../tests/fixtures/step_sentences.json'
import type { AcceptanceStep } from '../../lib/api'
import { describeStep } from './steps'

describe('describeStep', () => {
  it('says what the Playwright step title says, for every step in the fixture', () => {
    expect(fixture.length).toBeGreaterThan(0)
    for (const entry of fixture as { step: AcceptanceStep; sentence: string }[]) {
      expect(describeStep(entry.step)).toBe(entry.sentence)
    }
  })
})
