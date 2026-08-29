import { describe, expect, it } from 'vitest'

import type { RunEvent } from './api'
import { exhaustedOwners, reopenings } from './runs'

const event = (sequence: number, event_type: string, payload: Record<string, unknown> = {}): RunEvent =>
  ({ sequence, event_type, payload, task_id: null, created_at: '2026-08-29T00:00:00+00:00' }) as unknown as RunEvent

describe('a failed run names who used every attempt', () => {
  it('reads the last retry_withheld event', () => {
    const events = [
      event(1, 'task.reopened', { failed_node_id: 'app' }),
      event(2, 'task.retry_withheld', { exhausted_node_ids: ['web'] }),
      event(3, 'task.reopened', { failed_node_id: 'app' }),
      event(4, 'task.retry_withheld', { exhausted_node_ids: ['web', 'domain'] }),
    ]
    expect(exhaustedOwners(events)).toEqual(['web', 'domain'])
    expect(reopenings(events)).toBe(2)
  })

  it('names nobody when the run did not say', () => {
    expect(exhaustedOwners([event(1, 'task.failed', { summary: 'unit exited with 1' })])).toEqual([])
    expect(exhaustedOwners([event(1, 'task.retry_withheld', {})])).toEqual([])
    expect(reopenings([])).toBe(0)
  })
})
