import { publicAsset } from './publicAsset'

export const STAT_ICON_PATHS = {
  resources: publicAsset('/assets/ui/icons/resource.png'),
  population: publicAsset('/assets/ui/icons/population.png'),
} as const

export const PLAYER_STAT_ICON_PATHS = {
  damageDealt: publicAsset('/assets/ui/stats/damage-dealt.png'),
  damageReceived: publicAsset('/assets/ui/stats/damage-received.png'),
  unitsDestroyed: publicAsset('/assets/ui/stats/units-destroyed.png'),
  coresDestroyed: publicAsset('/assets/ui/stats/cores-destroyed.png'),
  harvested: publicAsset('/assets/ui/stats/harvested.png'),
  deposited: publicAsset('/assets/ui/stats/deposited.png'),
  beaconPickups: publicAsset('/assets/ui/stats/beacon-pickups.png'),
  beaconTicksHeld: publicAsset('/assets/ui/stats/beacon-held.png'),
  beaconBonusHarvested: publicAsset('/assets/ui/stats/beacon-bonus.png'),
  spawned: publicAsset('/assets/ui/stats/spawned.png'),
  lost: publicAsset('/assets/ui/stats/lost.png'),
  unitHPRecovered: publicAsset('/assets/ui/stats/damage-received.png'),
  coreHPRecovered: publicAsset('/assets/ui/stats/survival.png'),
  survival: publicAsset('/assets/ui/stats/survival.png'),
  respawns: publicAsset('/assets/ui/stats/respawns.png'),
} as const
