/** Small shared formatters. Extracted so a stage component can render a
 * status or an error without pulling in the whole control plane. */

import { V2ApiError } from './api'

export const lines = (value: string) =>
  value
    .split('\n')
    .map((item) => item.trim())
    .filter(Boolean)

export const commaList = (value: string) =>
  value
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean)

export const errorMessage = (error: unknown) =>
  error instanceof V2ApiError
    ? `${error.kind}${error.status ? ` (${error.status})` : ''}: ${error.message}`
    : error instanceof Error
      ? error.message
      : String(error)

export const shortId = (value: string | null | undefined) =>
  value ? (value.length > 26 ? `${value.slice(0, 13)}…${value.slice(-8)}` : value) : '—'

export const statusClass = (status: string) => {
  if (['ok', 'approved', 'ready', 'completed'].includes(status)) return 'ok'
  if (['rejected', 'failed', 'offline'].includes(status)) return 'bad'
  return 'warn'
}

