// Combat-squad membership served by the tactic dashboard (/api/teams). The
// arena sidebar uses it to show the same 守家/进攻/风筝 squads the dashboard
// manages, keyed by the dashboard display names (V1/R2...).
import type { TeamConfig } from './teamSettings'

export type TeamKey = 'home' | 'attack' | 'kite' | 'guerrilla' | 'unassigned'

export type TeamRoster = Record<string, TeamKey>

// Squad display order in the sidebar: the three primary squads first, then
// guerrilla / unassigned only when they hold members (rendering decides).
export const TEAM_KEYS: TeamKey[] = ['home', 'attack', 'kite', 'guerrilla', 'unassigned']

export interface TeamData {
  roster: TeamRoster
  config: TeamConfig
}

export async function loadTeamRoster(): Promise<TeamData> {
  try {
    const response = await fetch('/api/teams', { credentials: 'same-origin' })
    if (!response.ok) return { roster: {}, config: {} }
    const data = await response.json() as { ok?: boolean; combat_units?: unknown; config?: unknown }
    if (!data || data.ok !== true || !Array.isArray(data.combat_units)) return { roster: {}, config: {} }
    const roster: TeamRoster = {}
    for (const unit of data.combat_units as Array<Record<string, unknown>>) {
      const name = typeof unit.name === 'string' ? unit.name : ''
      const team = typeof unit.team === 'string' ? unit.team : ''
      if (!name) continue
      roster[name] = (TEAM_KEYS as string[]).includes(team) ? (team as TeamKey) : 'unassigned'
    }
    const config = data.config && typeof data.config === 'object' && !Array.isArray(data.config)
      ? data.config as TeamConfig
      : {}
    return { roster, config }
  } catch {
    // Outside the deployed dashboard (dev server / demo) the endpoint does
    // not exist — the squads tab simply stays empty.
    return { roster: {}, config: {} }
  }
}

// Dashboard display name -> squad key, with unknown units landing in the
// unassigned pool so no live combat unit ever disappears from the view.
export function teamOfName(name: string | undefined, roster: TeamRoster): TeamKey {
  if (!name) return 'unassigned'
  return roster[name] ?? 'unassigned'
}

// Move one unit into a squad, removing it from wherever it was before. The
// input is not mutated; callers can roll back by keeping the old roster.
export function moveUnitInRoster(roster: TeamRoster, name: string, team: TeamKey): TeamRoster {
  const next: TeamRoster = {}
  for (const [unit, current] of Object.entries(roster)) if (unit !== name) next[unit] = current
  next[name] = team
  return next
}

// Squad key -> tactic_config.json roster field. 'unassigned' has no field:
// dropping a unit there simply removes the name from every roster string.
const ROSTER_FIELDS: Array<[Exclude<TeamKey, 'unassigned'>, string]> = [
  ['home', 'home_team'],
  ['attack', 'attack_team'],
  ['kite', 'kite_team'],
  ['guerrilla', 'guerrilla_team'],
]

export function rosterPayload(roster: TeamRoster): Record<string, string> {
  const payload: Record<string, string> = {}
  for (const [team, field] of ROSTER_FIELDS) {
    payload[field] = Object.entries(roster).filter(([, value]) => value === team).map(([unit]) => unit).join(',')
  }
  return payload
}

// Persist a dragged assignment. POST only the roster fields: /api/teams
// merges partial updates, so combat settings (radii, modes...) stay
// untouched. The tactic engine picks the change up on the next tick.
export async function saveTeamRoster(roster: TeamRoster): Promise<boolean> {
  return postTeams(rosterPayload(roster))
}

// Persist one squad-setting change (partial update: the dashboard merges
// only the keys present, so rosters and other settings stay untouched).
export async function saveTeamSettings(patch: TeamConfig): Promise<boolean> {
  return postTeams(patch)
}

async function postTeams(payload: Record<string, unknown>): Promise<boolean> {
  try {
    const response = await fetch('/api/teams', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify(payload),
    })
    if (!response.ok) return false
    const data = await response.json() as { ok?: boolean }
    return data?.ok === true
  } catch {
    return false
  }
}
