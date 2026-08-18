import { useCallback, useEffect, useRef, useState } from 'react'
import { loadTeamRoster, moveUnitInRoster, saveTeamRoster, type TeamKey, type TeamRoster } from '../lib/teamRoster'

// Combat-squad rosters (守家/进攻/风筝/游击) come from the tactic dashboard's
// /api/teams endpoint; refresh on every new tick so roster edits and fresh
// spawns show up in the arena sidebar without a manual reload.
export function useUnitTeams(tick: number | null, enabled = true) {
  const [roster, setRoster] = useState<TeamRoster>({})
  const rosterRef = useRef(roster)
  useEffect(() => { rosterRef.current = roster }, [roster])
  useEffect(() => {
    if (!enabled) return
    let cancelled = false
    void loadTeamRoster().then((next) => { if (!cancelled) setRoster(next) })
    return () => { cancelled = true }
  }, [tick, enabled])
  // Drag & drop assignment: the chip moves instantly (optimistic), the save
  // POSTs right away so it takes effect from the next tick, and a failed
  // save rolls the sidebar back to the previous roster.
  const assignTeam = useCallback((name: string, team: TeamKey) => {
    const current = rosterRef.current
    if (!enabled || !name || current[name] === team) return
    const next = moveUnitInRoster(current, name, team)
    setRoster(next)
    void saveTeamRoster(next).then((ok) => { if (!ok) setRoster(current) })
  }, [enabled])
  return { roster, assignTeam }
}
