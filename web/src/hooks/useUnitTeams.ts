import { useEffect, useState } from 'react'
import { loadTeamRoster, type TeamRoster } from '../lib/teamRoster'

// Combat-squad rosters (守家/进攻/风筝/游击) come from the tactic dashboard's
// /api/teams endpoint; refresh on every new Tick so roster edits and fresh
// spawns show up in the arena sidebar without a manual reload.
export function useUnitTeams(tick: number | null, enabled = true) {
  const [roster, setRoster] = useState<TeamRoster>({})
  useEffect(() => {
    if (!enabled) return
    let cancelled = false
    void loadTeamRoster().then((next) => { if (!cancelled) setRoster(next) })
    return () => { cancelled = true }
  }, [tick, enabled])
  return roster
}
