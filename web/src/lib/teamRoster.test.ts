import { describe, expect, it } from 'vitest'
import { moveUnitInRoster, rosterPayload, teamOfName, type TeamRoster } from './teamRoster'

describe('teamOfName', () => {
  it('falls back to the standby pool for unknown or missing names', () => {
    expect(teamOfName(undefined, { V1: 'home' })).toBe('unassigned')
    expect(teamOfName('R9', { V1: 'home' })).toBe('unassigned')
    expect(teamOfName('V1', { V1: 'home' })).toBe('home')
  })
})

describe('moveUnitInRoster', () => {
  it('moves the unit into the target squad and leaves everyone else untouched', () => {
    const roster: TeamRoster = { V1: 'home', R1: 'kite', V2: 'attack' }
    const next = moveUnitInRoster(roster, 'V1', 'attack')
    expect(next).toEqual({ V1: 'attack', R1: 'kite', V2: 'attack' })
    // The input stays untouched so callers can roll back on save failure.
    expect(roster.V1).toBe('home')
  })

  it('records the unit as unassigned when dropped into the standby pool', () => {
    const next = moveUnitInRoster({ V1: 'home' }, 'V1', 'unassigned')
    expect(next.V1).toBe('unassigned')
  })
})

describe('rosterPayload', () => {
  it('serialises each squad as a comma-joined name list, omitting the standby pool', () => {
    expect(rosterPayload({ V1: 'home', R1: 'home', V2: 'attack', R9: 'unassigned' })).toEqual({
      home_team: 'V1,R1',
      attack_team: 'V2',
      kite_team: '',
      guerrilla_team: '',
    })
  })

  it('emits empty strings for squads without members', () => {
    expect(rosterPayload({})).toEqual({ home_team: '', attack_team: '', kite_team: '', guerrilla_team: '' })
  })
})
