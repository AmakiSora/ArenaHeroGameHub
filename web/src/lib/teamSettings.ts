// Per-squad combat parameters mirrored from the dashboard's 战斗分队 panel.
// Field labels/hints/ranges duplicate render_teams_panel() in dashboard.py;
// they are POSTed back to /api/teams, which validates ranges server-side.
export type TeamConfig = Record<string, number | boolean | string>

export type ModeValue = 'coords' | 'auto' | 'beacon'

export type TeamSettingField =
  // pickYField pairs an X input with its Y field for map picking (⌖), the
  // same pairing the dashboard's pick buttons fill in one map click.
  | { kind: 'number'; field: string; labelKey: string; hintKey?: string; min: number; max: number; step?: number; modes?: ModeValue[]; pickYField?: string }
  | { kind: 'mode'; field: string }
  | { kind: 'select'; field: string; labelKey: string; hintKey?: string; options: number[] }
  | { kind: 'switch'; field: string; labelKey: string; hintKey?: string }

export interface SquadSettingsSpec {
  titleKey: string
  subtitleKey?: string
  // Mode radio field (attack_mode / kite_mode); fields carrying `modes`
  // only render while that mode is active, mirroring the dashboard form.
  modeField?: string
  beaconNoteKey?: string
  fields: TeamSettingField[]
}

export const TEAM_SETTINGS: Partial<Record<string, SquadSettingsSpec>> = {
  home: {
    titleKey: 'homeTitle',
    subtitleKey: 'homeSubtitle',
    fields: [
      { kind: 'number', field: 'home_patrol_radius', labelKey: 'patrolRadius', hintKey: 'patrolRadiusHint', min: 1, max: 30 },
      { kind: 'number', field: 'home_engage_radius', labelKey: 'engageRadius', hintKey: 'engageRadiusHint', min: 0, max: 30 },
      { kind: 'number', field: 'home_engage_memory_ticks', labelKey: 'memoryTicks', hintKey: 'memoryTicksHint', min: 0, max: 20 },
      { kind: 'number', field: 'combat_heal_hp_threshold', labelKey: 'healThreshold', hintKey: 'healThresholdHint', min: 0, max: 4 },
      { kind: 'number', field: 'combat_heal_return_limit', labelKey: 'healReturnLimit', hintKey: 'healReturnLimitHint', min: 0, max: 19 },
    ],
  },
  attack: {
    titleKey: 'attackTitle',
    subtitleKey: 'attackSubtitle',
    modeField: 'attack_mode',
    beaconNoteKey: 'attackBeaconNote',
    fields: [
      { kind: 'mode', field: 'attack_mode' },
      { kind: 'number', field: 'attack_target_x', labelKey: 'targetX', min: -10000, max: 10000, modes: ['coords'], pickYField: 'attack_target_y' },
      { kind: 'number', field: 'attack_target_y', labelKey: 'targetY', min: -10000, max: 10000, modes: ['coords'] },
      { kind: 'number', field: 'attack_auto_radius', labelKey: 'autoRadius', hintKey: 'autoRadiusHint', min: 0, max: 1000, modes: ['auto'] },
      { kind: 'number', field: 'attack_retreat_radius', labelKey: 'retreatRadius', hintKey: 'retreatRadiusHint', min: 0, max: 30, modes: ['auto'] },
      { kind: 'number', field: 'attack_march_engage_radius', labelKey: 'marchRadius', hintKey: 'marchRadiusHint', min: 0, max: 500, modes: ['coords', 'beacon'] },
      // 通用战斗（全队生效）lives with the attack squad: the sidebar has no
      // shared panel, so it stays reachable here, flagged via the subtitle.
      { kind: 'select', field: 'ranger_attack_range', labelKey: 'rangerRange', hintKey: 'rangerRangeHint', options: [1, 2, 3] },
      { kind: 'switch', field: 'ranger_lead_fire_enabled', labelKey: 'leadFire', hintKey: 'leadFireHint' },
    ],
  },
  kite: {
    titleKey: 'kiteTitle',
    subtitleKey: 'kiteSubtitle',
    modeField: 'kite_mode',
    beaconNoteKey: 'kiteBeaconNote',
    fields: [
      { kind: 'mode', field: 'kite_mode' },
      { kind: 'number', field: 'kite_target_x', labelKey: 'targetX', min: -10000, max: 10000, modes: ['coords'], pickYField: 'kite_target_y' },
      { kind: 'number', field: 'kite_target_y', labelKey: 'targetY', min: -10000, max: 10000, modes: ['coords'] },
      { kind: 'number', field: 'kite_auto_radius', labelKey: 'autoRadius', hintKey: 'kiteAutoRadiusHint', min: 0, max: 1000, modes: ['auto'] },
      { kind: 'number', field: 'kite_march_engage_radius', labelKey: 'marchRadius', hintKey: 'kiteMarchRadiusHint', min: 0, max: 500, modes: ['coords', 'beacon'] },
      { kind: 'number', field: 'combat_heal_hp_threshold', labelKey: 'healThreshold', hintKey: 'healThresholdHint', min: 0, max: 4 },
      { kind: 'number', field: 'combat_heal_return_limit', labelKey: 'healReturnLimit', hintKey: 'healReturnLimitHint', min: 0, max: 19 },
    ],
  },
  guerrilla: {
    titleKey: 'guerrillaTitle',
    subtitleKey: 'guerrillaSubtitle',
    fields: [
      { kind: 'number', field: 'guerrilla_engage_radius', labelKey: 'guerrillaRadius', hintKey: 'guerrillaRadiusHint', min: 0, max: 30 },
    ],
  },
}

// Worker strategy mirrors the dashboard's 工人与寻路 config group; it is
// saved via /api/config (strategy config), unlike squad fields which go to
// /api/teams.
export const WORKER_SETTINGS: SquadSettingsSpec = {
  titleKey: 'workerTitle',
  subtitleKey: 'workerSubtitle',
  fields: [
    { kind: 'switch', field: 'worker_bfs_enabled', labelKey: 'bfsEnabled', hintKey: 'bfsEnabledHint' },
    { kind: 'number', field: 'bfs_max_steps', labelKey: 'bfsMaxSteps', hintKey: 'bfsMaxStepsHint', min: 50, max: 8000, step: 50 },
    { kind: 'switch', field: 'avoid_backtracking', labelKey: 'avoidBacktracking', hintKey: 'avoidBacktrackingHint' },
    { kind: 'number', field: 'backtrack_penalty', labelKey: 'backtrackPenalty', hintKey: 'backtrackPenaltyHint', min: 0, max: 100 },
    { kind: 'number', field: 'enemy_threat_radius', labelKey: 'enemyThreatRadius', hintKey: 'enemyThreatRadiusHint', min: 0, max: 10 },
    { kind: 'number', field: 'worker_mine_max_distance', labelKey: 'mineMaxDistance', hintKey: 'mineMaxDistanceHint', min: 0, max: 200 },
    { kind: 'switch', field: 'worker_explore_when_full', labelKey: 'exploreWhenFull', hintKey: 'exploreWhenFullHint' },
  ],
}

// Settings panel lookup for both sidebar views: squad keys (teams view) and
// the WORKER unit-group key (groups view). Everything else has no panel.
export function settingsSpecFor(key: string): SquadSettingsSpec | undefined {
  if (key === 'WORKER') return WORKER_SETTINGS
  return TEAM_SETTINGS[key]
}

// Every field owned by the squad panels; they save through /api/teams while
// all other fields (worker strategy) go to /api/config.
export const TEAM_SETTING_FIELDS: ReadonlySet<string> = new Set(
  Object.values(TEAM_SETTINGS).flatMap((spec) => (spec ? spec.fields.map((field) => field.field) : [])),
)

const MODE_OPTIONS: ModeValue[] = ['coords', 'auto', 'beacon']

export { MODE_OPTIONS }
