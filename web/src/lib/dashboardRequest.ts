import { apiURL } from './api'

// One request helper for every tactic-dashboard endpoint the arena polls
// (eight per tick before the review). It centralises the boilerplate the
// modules used to copy: same-origin credentials, a hard timeout, the caller's
// AbortSignal, JSON POST bodies, and a discriminated outcome so consumers can
// tell "no data" from "the dashboard answered 401/500" instead of rendering an
// empty panel.
export type DashboardRequestKind = 'aborted' | 'http' | 'network'

export interface DashboardRequestFailure {
  kind: DashboardRequestKind
  path: string
  // HTTP status for kind === 'http'; the fetch/parse message otherwise.
  status?: number
  error?: string
}

export type DashboardResult<T> =
  | { ok: true; data: T }
  | { ok: false; failure: DashboardRequestFailure }

export interface DashboardRequestOptions {
  signal?: AbortSignal
  // Defaults to 15 s: a hung dashboard must never wedge a poller.
  timeoutMs?: number
  method?: 'GET' | 'POST'
  body?: unknown
}

const DASHBOARD_TIMEOUT_MS = 15_000

export async function dashboardRequest<T>(path: string, options: DashboardRequestOptions = {}): Promise<DashboardResult<T>> {
  const { signal, timeoutMs = DASHBOARD_TIMEOUT_MS, method = 'GET', body } = options
  const headers: Record<string, string> = {}
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  let response: Response
  try {
    response = await fetch(apiURL(path), {
      method,
      credentials: 'same-origin',
      headers,
      ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
      signal: combineSignals(signal, timeoutSignal(timeoutMs)),
    })
  } catch (cause) {
    // A caller-cancelled request is expected (tick change / tab hidden) and
    // must not be reported as a dashboard failure.
    if (signal?.aborted) return { ok: false, failure: { kind: 'aborted', path } }
    const error = cause instanceof Error ? cause.message : undefined
    console.warn('[dashboard] request failed', path, error)
    return { ok: false, failure: { kind: 'network', path, ...(error ? { error } : {}) } }
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => ({})) as { error?: string }
    console.warn('[dashboard] request failed', path, response.status)
    return { ok: false, failure: { kind: 'http', path, status: response.status, ...(payload.error ? { error: payload.error } : {}) } }
  }
  if (response.status === 204) return { ok: true, data: undefined as T }
  try {
    return { ok: true, data: await response.json() as T }
  } catch (cause) {
    const error = cause instanceof Error ? cause.message : undefined
    console.warn('[dashboard] request failed', path, error ?? 'INVALID_JSON')
    return { ok: false, failure: { kind: 'network', path, ...(error ? { error } : {}) } }
  }
}

// The caller's signal (per-tick cancellation) combined with the request
// deadline. AbortSignal.any() exists in current browsers and Node; the manual
// fallback keeps older jsdom-based test runners working.
function combineSignals(caller: AbortSignal | undefined, timeout: AbortSignal): AbortSignal {
  if (!caller) return timeout
  if (typeof AbortSignal.any === 'function') return AbortSignal.any([caller, timeout])
  const controller = new AbortController()
  const abort = () => {
    caller.removeEventListener('abort', abort)
    timeout.removeEventListener('abort', abort)
    controller.abort()
  }
  if (caller.aborted || timeout.aborted) { controller.abort(); return controller.signal }
  caller.addEventListener('abort', abort, { once: true })
  timeout.addEventListener('abort', abort, { once: true })
  return controller.signal
}

function timeoutSignal(timeoutMs: number): AbortSignal {
  if (typeof AbortSignal.timeout === 'function') return AbortSignal.timeout(timeoutMs)
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  // Do not keep a Node process (vitest) alive just for a deadline timer.
  const unrefable = timer as unknown as { unref?: () => void }
  if (typeof unrefable.unref === 'function') unrefable.unref()
  return controller.signal
}
