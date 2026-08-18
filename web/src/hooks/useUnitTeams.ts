import { useCallback, useEffect, useRef, useState } from 'react'
import { loadTeamRoster, moveUnitInRoster, saveTeamRoster, saveTeamSettings, type TeamKey, type TeamRoster } from '../lib/teamRoster'
import type { TeamConfig } from '../lib/teamSettings'

// Combat-squad rosters (守家/进攻/风筝/游击) and squad settings come from the
// tactic dashboard's /api/teams endpoint; refresh on every new tick so roster
// edits and fresh spawns show up in the arena sidebar without a reload.
export function useUnitTeams(tick: number | null, enabled = true) {
  const [roster, setRoster] = useState<TeamRoster>({})
  const [config, setConfig] = useState<TeamConfig>({})
  const rosterRef = useRef(roster)
  const configRef = useRef(config)
  useEffect(() => { rosterRef.current = roster }, [roster])
  useEffect(() => { configRef.current = config }, [config])
  useEffect(() => {
    if (!enabled) return
    let cancelled = false
    void loadTeamRoster().then((next) => { if (!cancelled) { setRoster(next.roster); setConfig(next.config) } })
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
  // Squad setting change: same optimistic pattern — apply locally, POST the
  // single field, roll back on failure. The server validates ranges and
  // answers 400 on invalid input, which maps to a rollback here.
  const updateConfig = useCallback((field: string, value: number | boolean | string) => {
    const current = configRef.current
    if (!enabled || current[field] === value) return
    const next = { ...current, [field]: value }
    setConfig(next)
    void saveTeamSettings({ [field]: value }).then((ok) => { if (!ok) setConfig(current) })
  }, [enabled])
  return { roster, config, assignTeam, updateConfig }
}
