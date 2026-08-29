import type { RunEvent } from './api'

/**
 * Who a failed run says to rebuild: a browser scenario that fails on a page
 * another component owns reopens that component; when the owners have no
 * attempts left the engine withholds the retry and names them. The panel
 * reads that event rather than guessing from the task that ran the browser.
 */
export function exhaustedOwners(events: RunEvent[]): string[] {
  const withheld = [...events].reverse().find((event) => event.event_type === 'task.retry_withheld')
  const ids = withheld?.payload.exhausted_node_ids
  return Array.isArray(ids) ? ids.map(String) : []
}

/** How many times a component was reopened during the run. */
export const reopenings = (events: RunEvent[]): number =>
  events.filter((event) => event.event_type === 'task.reopened').length
