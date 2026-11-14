import type { CommandPlan, Leaderboard, PlayerStats, Receipt, Session, User } from './types'

export class APIError extends Error {
  constructor(
    public readonly code: string,
    public readonly status: number,
    message?: string,
  ) {
    super(message || code)
  }
}

// Same-origin deployment: the ArenaGame dashboard serves this app under /arena
// and proxies /api/v1/* to api.arenahero.io. Two credential paths coexist:
//  - The login session cookie (rewritten to the dashboard origin by the
//    proxy). Commands sent with it land in the MANUAL plan slot and override
//    the bot's AGENT plan.
//  - The server-side API key injected by the proxy when no session exists;
//    those commands land in the AGENT slot (same slot the tactic uses).
const csrfKey = 'arena-hero.csrf'

export const getCSRF = () => localStorage.getItem(csrfKey) ?? ''
export const setCSRF = (token: string) => localStorage.setItem(csrfKey, token)
export const clearCSRF = () => localStorage.removeItem(csrfKey)

// The pasted official-site credentials stay valid upstream until that session
// expires, and re-importing them is idempotent. The draft lives on the
// dashboard server (behind DASHBOARD_TOKEN), never in browser storage where
// any XSS could read raw session cookies. A pre-existing localStorage draft
// is migrated server-side once, then deleted locally.
const legacyImportDraftKey = 'arena-hero.import-draft'

export interface ImportDraft { cookies: string; csrf: string }

function parseDraft(value: unknown): ImportDraft | null {
  if (!value || typeof value !== 'object') return null
  const parsed = value as Partial<ImportDraft>
  if (typeof parsed.cookies === 'string' && typeof parsed.csrf === 'string' && parsed.cookies.trim() && parsed.csrf.trim()) {
    return { cookies: parsed.cookies, csrf: parsed.csrf }
  }
  return null
}

function readLegacyDraft(): ImportDraft | null {
  try {
    return parseDraft(JSON.parse(localStorage.getItem(legacyImportDraftKey) ?? 'null'))
  } catch {
    return null
  }
}

export async function fetchImportDraft(): Promise<ImportDraft | null> {
  const legacy = readLegacyDraft()
  if (legacy) {
    try {
      await request<{ ok: boolean }>('/api/session/draft', { method: 'POST', body: JSON.stringify(legacy) })
      localStorage.removeItem(legacyImportDraftKey)
    } catch {
      // Dashboard unreachable (dev/demo): keep the legacy draft for prefill
      // and retry the migration on the next visit.
    }
    return legacy
  }
  try {
    const data = await request<{ ok: boolean; draft: unknown }>('/api/session/draft')
    return parseDraft(data.draft)
  } catch {
    return null
  }
}

export async function clearImportDraft(): Promise<void> {
  localStorage.removeItem(legacyImportDraftKey)
  try {
    await request('/api/session/draft/clear', { method: 'POST' })
  } catch {
    // Best effort: the server draft is also replaced by the next import.
  }
}

// crypto.randomUUID() exists only in secure contexts (https / localhost).
// The dashboard is served over plain http, where it is undefined and every
// command click threw before fetch even ran — so the Idempotency-Key needs a
// fallback. 8-128 visible ASCII bytes is the only upstream constraint.
function idempotencyKey(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return crypto.randomUUID()
  const random = () => Math.random().toString(36).slice(2, 10)
  return `manual-${Date.now().toString(36)}-${random()}${random()}`
}

export function apiURL(path: string, baseURL = import.meta.env.VITE_API_BASE_URL ?? '') {
  return `${baseURL.trim().replace(/\/+$/, '')}${path}`
}

// A hung request must never wedge the serial command-submit queue: every
// call gets a hard deadline and rejects with REQUEST_FAILED on timeout.
const REQUEST_TIMEOUT_MS = 15_000

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  if (init.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  let response: Response
  try {
    response = await fetch(apiURL(path), { ...init, headers, credentials: 'include', signal: init.signal ?? AbortSignal.timeout(REQUEST_TIMEOUT_MS) })
  } catch (cause) {
    // fetch itself threw: the request never reached the dashboard. Surface
    // the browser's reason (mixed content, https upgrade, net failure,
    // timeout) — it is the decisive clue, far more than a bare code.
    const detail = cause instanceof Error && cause.message ? ` ${cause.message}` : ''
    throw new APIError(`REQUEST_FAILED${detail}`, 0)
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({})) as { error?: string; message?: string }
    throw new APIError(body.error ?? 'REQUEST_FAILED', response.status, body.message)
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export const api = {
  leaderboard: () => request<Leaderboard>('/api/v1/leaderboard'),
  me: () => request<User>('/api/v1/me'),
  stats: () => request<PlayerStats>('/api/v1/me/stats'),
  login: async (email: string, password: string) => {
    const session = await request<Session>('/api/v1/auth/login', { method: 'POST', body: JSON.stringify({ email, password }) })
    setCSRF(session.csrf_token)
    return session
  },
  logout: async () => {
    await request<void>('/api/v1/auth/logout', { method: 'POST', headers: { 'X-CSRF-Token': getCSRF() } })
    clearCSRF()
    // Logout invalidates the session upstream, so the remembered credentials
    // would only fail on the next silent re-import — drop them too.
    await clearImportDraft()
  },
  // Dashboard-local: OAuth-only accounts (LINUX DO / GitHub) cannot log in
  // through the proxy, so their official-site session cookie is imported,
  // validated upstream and rebound to this origin. The CSRF token (copied
  // from the official site's localStorage) is mandatory for the session
  // credential's MANUAL command POSTs; the dashboard keeps it in a
  // proxy-owned cookie and remembers the import draft server-side.
  importSession: async (cookies: string, csrf: string) => {
    const result = await request<{ ok: boolean; user: User; csrf_token: string }>('/api/v1/session/import', {
      method: 'POST',
      body: JSON.stringify({ cookies, csrf }),
    })
    if (result.csrf_token) setCSRF(result.csrf_token)
    return result
  },
  submitCommands: (plan: CommandPlan) => request<Receipt>('/api/v1/game/commands', {
    method: 'POST',
    headers: { 'X-CSRF-Token': getCSRF(), 'Idempotency-Key': idempotencyKey() },
    body: JSON.stringify(plan),
  }),
}