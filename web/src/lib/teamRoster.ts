// Combat-squad membership served by the tactic dashboard (/api/teams). The
// arena sidebar uses it to show the same 守家/进攻/风筝 squads the dashboard
// manages, keyed by the dashboard display names (V1/R2...).
export type TeamKey = 'home' | 'attack' | 'kite' | 'guerrilla' | 'unassigned'

export type TeamRoster = Record<string, TeamKey>

// Squad display order in the sidebar: the three primary squads first, then
// guerrilla / unassigned only when they hold members (rendering decides).
export const TEAM_KEYS: TeamKey[] = ['home', 'attack', 'kite', 'guerrilla', 'unassigned']

export async function loadTeamRoster(): Promise<TeamRoster> {
  try {
    const response = await fetch('/api/teams', { credentials: 'same-origin' })
    if (!response.ok) return {}
    const data = await response.json() as { ok?: boolean; combat_units?: unknown }
    if (!data || data.ok !== true || !Array.isArray(data.combat_units)) return {}
    const roster: TeamRoster = {}
    for (const unit of data.combat_units as Array<Record<string, unknown>>) {
      const name = typeof unit.name === 'string' ? unit.name : ''
      const team = typeof unit.team === 'string' ? unit.team : ''
      if (!name) continue
      roster[name] = (TEAM_KEYS as string[]).includes(team) ? (team as TeamKey) : 'unassigned'
    }
    return roster
  } catch {
    // Outside the deployed dashboard (dev server / demo) the endpoint does
    // not exist — the squads tab simply stays empty.
    return {}
  }
}

// Dashboard display name -> squad key, with unknown units landing in the
// unassigned pool so no live combat unit ever disappears from the view.
export function teamOfName(name: string | undefined, roster: TeamRoster): TeamKey {
  if (!name) return 'unassigned'
  return roster[name] ?? 'unassigned'
}
