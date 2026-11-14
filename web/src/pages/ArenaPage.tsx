import { Crosshair, Footprints, Move, Settings, Sword, Users } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { AssetList } from '../components/game/AssetList'
import { BattleLogPanel } from '../components/game/BattleLogPanel'
import { EnemySightings } from '../components/game/EnemySightings'
import { GameHUD } from '../components/game/GameHUD'
import { GuerrillaTracksDialog } from '../components/game/GuerrillaTracksDialog'
import { ENEMY_FILTER_TYPES, MapControls } from '../components/game/MapControls'
import { PendingCommands } from '../components/game/PendingCommands'
import { ResourceActivity } from '../components/game/ResourceActivity'
import { RespawnOverlay } from '../components/game/RespawnOverlay'
import { StrategyConfigDialog } from '../components/game/StrategyConfigDialog'
import { WorldCanvas } from '../components/game/WorldCanvas'
import { UnitActionDialog, type MapAnchor } from '../components/game/UnitActionDialog'
import { useGameStream } from '../hooks/useGameStream'
import { useGuerrillaTracks } from '../hooks/useGuerrillaTracks'
import { useEnemyMemory } from '../hooks/useEnemyMemory'
import { useUnitNames } from '../hooks/useUnitNames'
import { useUnitRoutes } from '../hooks/useUnitRoutes'
import { useUnitTeams } from '../hooks/useUnitTeams'
import { useWaypoints } from '../hooks/useWaypoints'
import { useSquadTargets } from '../hooks/useSquadTargets'
import { useHolds } from '../hooks/useHolds'
import { useAuth } from '../context/AuthContext'
import type { EnemySightingType } from '../lib/enemyMemory'
import type { UnitRoute } from '../lib/unitRoutes'
import { addUnitWaypoint, removeUnitWaypoint, type WaypointMode } from '../lib/waypoints'
import { addSquadTarget, clearSquadTargets, removeSquadTarget, type SquadKey } from '../lib/squadTargets'
import { clearUnitHold, setUnitHold } from '../lib/holds'
import { saveStrategyValues } from '../lib/strategyConfig'
import { unitDashboardName } from '../lib/unitNames'
import { plannedShotMarkers, plannedSweepMarkers, rangerAttackOptions, vanguardAttackOptions } from '../lib/combatPreview'
import { directionTo, moveTargets, plannedMoveArrows } from '../lib/movementPreview'
import { getErrorMessage } from '../lib/errorMessage'
import { getActionAvailability } from '../lib/actionAvailability'
import { coreDestructionFromEvents } from '../lib/destruction'
import { applyAutonomousMovement, createNavigationContextCached, findMovementPath, reachableMovementDestinations, readMovementGoals, type MovementGoals, type MovementRoute, type PathFailure } from '../lib/pathfinding'
import { mergeCommandPlans, prepareManualUnitActionPlan } from '../lib/commandPlans'
import type { CommandPlan, CoreAction, Position, UnitAction, WorldObject } from '../lib/types'
import { positionKey } from '../lib/visibility'
import { WORLD_BACKGROUND_PATH } from '../lib/worldArt'

// Cap for the top-left remembered-enemy strip: 21 chips fill exactly three
// rows of seven. The cap applies to whatever type-filter combination is
// currently enabled, ranked by recency, so it is computed after filtering.
const MEMORY_SIGHTINGS_MAX = 21
// Stable empty array so the memoized WorldCanvas does not see a fresh route
// list on every page render while the layer is hidden.
const EMPTY_UNIT_ROUTES: UnitRoute[] = []
const EMPTY_MOVEMENT_ROUTES: MovementRoute[] = []

// The autonomous pass returns fresh route objects every tick; only replace the
// stored list when something actually changed so the canvas does not get a new
// identity (and an extra render) for an idle tick.
function sameMovementRoutes(left: MovementRoute[], right: MovementRoute[]): boolean {
  if (left === right) return true
  if (left.length !== right.length) return false
  return left.every((route, index) => {
    const other = right[index]
    return route.objectId === other.objectId
      && route.blocked === other.blocked
      && route.destination[0] === other.destination[0] && route.destination[1] === other.destination[1]
      && route.path.length === other.path.length
      && route.path.every((point, pathIndex) => point[0] === other.path[pathIndex][0] && point[1] === other.path[pathIndex][1])
  })
}

// Show the raw upstream error code alongside the friendly text: it is the
// decisive clue when an order is rejected (CSRF_INVALID, TICK_MISMATCH...).
const describeError = (code: string) => `${getErrorMessage(code)} [${code}]`

export function ArenaPage({ demo = false }: { demo?: boolean }) {
  const { t } = useTranslation(); const { user } = useAuth(); const game = useGameStream(demo, demo ? 'demo' : user?.username ?? 'anonymous')
  // Tactic-dashboard display names (W1/V2/R3) so the arena page labels units
  // exactly like the dashboard; the demo has no dashboard backend behind it.
  const unitNames = useUnitNames(game.tick, !demo)
  // Combat-squad membership (守家/进攻/风筝...) from the tactic dashboard so
  // the sidebar's squads tab mirrors the dashboard's team board, with
  // drag-and-drop reassignment saved straight back to it.
  const { roster: teamRoster, config: teamConfig, assignTeam, updateConfig } = useUnitTeams(game.tick, !demo)
  // Remembered enemies (last-known positions) from the tactic bot's map
  // memory — the same 敌人踪迹 layer the dashboard map draws.
  const enemyMemory = useEnemyMemory(game.tick, !demo)
  // Manual per-unit target queues (the dashboard's 手动目标 panel) — the unit
  // dialog edits them through /api/waypoint/* exactly like the dashboard.
  const { waypoints, refresh: refreshWaypoints } = useWaypoints(game.tick, !demo)
  // Attack/kite squad target queues (进攻目标队列): the sidebar squad panels
  // edit them through /api/squad-target/*; the bot takes them one by one.
  const { squadTargets, refresh: refreshSquadTargets } = useSquadTargets(game.tick, !demo)
  // Manual per-unit hold position (the dashboard's 驻守 toggle) — a held unit
  // stands in place and auto-attacks enemies entering its range.
  const { holds, refresh: refreshHolds } = useHolds(game.tick, !demo)
  const submitGamePlan = game.submit
  const movementStorageKey = `arena-hero.movement-goals.${demo ? 'demo' : user?.username ?? 'anonymous'}`
  const [selectedId, setSelectedId] = useState<string | null>(null); const [targetMode, setTargetMode] = useState<'SHOOT' | 'SWEEP' | null>(null); const [moveSelecting, setMoveSelecting] = useState(false)
  // Mobile fleet drawer: the sidebar is lg-only, so below that breakpoint a
  // floating button opens the same AssetList as an overlay drawer.
  const [fleetOpen, setFleetOpen] = useState(false)
  // Squad target map picking (⌖ in the sidebar squad settings): while set,
  // the next map click fills that squad's target X/Y via /api/teams.
  const [coordPick, setCoordPick] = useState<{ xField: string; yField: string } | null>(null)
  // Manual-target map picking (选择目标点 in the unit dialog): the next map
  // click appends that point to the unit's waypoint queue.
  const [waypointPick, setWaypointPick] = useState<{ name: string; mode: WaypointMode } | null>(null)
  // Squad target-queue picking (目标队列 ⌖ in the squad settings): the next
  // map click appends that point to the squad's attack-target queue.
  const [squadTargetPick, setSquadTargetPick] = useState<SquadKey | null>(null)
  // Strategy settings dialog (配置 button, bottom-right): core/runtime/
  // production fields the sidebar panels don't already cover.
  const [strategyConfigOpen, setStrategyConfigOpen] = useState(false)
  const strategyConfigButtonRef = useRef<HTMLButtonElement>(null)
  // Guerrilla route overview (路线记录 button, next to the gear): a whole day of
  // exploration lines from the tactic bot's route recorder. Fetched only while
  // the dialog is open — a day file is thousands of cells per unit.
  const [guerrillaTracksOpen, setGuerrillaTracksOpen] = useState(false)
  const guerrillaTracksButtonRef = useRef<HTMLButtonElement>(null)
  const [guerrillaTracksDate, setGuerrillaTracksDate] = useState<string | null>(null)
  const guerrillaTracks = useGuerrillaTracks(guerrillaTracksOpen, guerrillaTracksDate)
  // Core-target map picking started from the strategy dialog: the modal
  // closes while picking (the map must be clickable) and reopens after the
  // chosen point is saved.
  const [strategyPick, setStrategyPick] = useState<{ xField: string; yField: string } | null>(null)
  const [movementError, setMovementError] = useState<PathFailure | null>(null)
  const [anchor, setAnchor] = useState<MapAnchor | null>(null)
  const destroyerStorageKey = `arena-hero.core-destroyer.${demo ? 'demo' : user?.username ?? 'anonymous'}`
  const selfDestructStorageKey = `arena-hero.core-self-destructed.${demo ? 'demo' : user?.username ?? 'anonymous'}`
  const enemyMemoryStorageKey = `arena-hero.enemy-memory-visible.${demo ? 'demo' : user?.username ?? 'anonymous'}`
  const enemyFilterStorageKey = `arena-hero.enemy-memory-filters.${demo ? 'demo' : user?.username ?? 'anonymous'}`
  const beaconIndicatorStorageKey = `arena-hero.beacon-indicator-visible.${demo ? 'demo' : user?.username ?? 'anonymous'}`
  const coreIndicatorStorageKey = `arena-hero.core-indicator-visible.${demo ? 'demo' : user?.username ?? 'anonymous'}`
  const unitRoutesStorageKey = `arena-hero.unit-routes-visible.${demo ? 'demo' : user?.username ?? 'anonymous'}`
  const obstaclesStorageKey = `arena-hero.obstacles-visible.${demo ? 'demo' : user?.username ?? 'anonymous'}`
  const [coreDestroyer, setCoreDestroyer] = useState<string | null>(() => sessionStorage.getItem(destroyerStorageKey))
  const [coreSelfDestructed, setCoreSelfDestructed] = useState(() => sessionStorage.getItem(selfDestructStorageKey) === 'true')
  const [enemyMemoryVisible, setEnemyMemoryVisible] = useState(() => localStorage.getItem(enemyMemoryStorageKey) !== 'false')
  const [beaconIndicatorVisible, setBeaconIndicatorVisible] = useState(() => localStorage.getItem(beaconIndicatorStorageKey) !== 'false')
  const [coreIndicatorVisible, setCoreIndicatorVisible] = useState(() => localStorage.getItem(coreIndicatorStorageKey) !== 'false')
  // The route filter starts hidden: it is an analysis overlay, not the
  // default view (unlike enemy memory, which operators usually want on).
  const [unitRoutesVisible, setUnitRoutesVisible] = useState(() => localStorage.getItem(unitRoutesStorageKey) === 'true')
  // Obstacle sprites start shown; operators with a large explored area can
  // hide the layer to trade scenery for frame rate.
  const [obstaclesVisible, setObstaclesVisible] = useState(() => localStorage.getItem(obstaclesStorageKey) !== 'false')
  // Per-unit destinations + planned routes (the dashboard's 路径 layer) for
  // the map filter button; only fetched while the layer is shown.
  const unitRoutes = useUnitRoutes(game.tick, !demo && unitRoutesVisible)
  const [enemyMemoryFilters, setEnemyMemoryFilters] = useState<EnemySightingType[]>(() => {
    try {
      const raw = localStorage.getItem(enemyFilterStorageKey)
      if (!raw) return [...ENEMY_FILTER_TYPES]
      const saved = JSON.parse(raw)
      if (!Array.isArray(saved)) return [...ENEMY_FILTER_TYPES]
      return ENEMY_FILTER_TYPES.filter((type) => saved.includes(type))
    } catch {
      return [...ENEMY_FILTER_TYPES]
    }
  })
  const [centerRequest, setCenterRequest] = useState(0); const [zoomRequest, setZoomRequest] = useState(0)
  const [centerPosition, setCenterPosition] = useState<Position | null>(null)
  const [plan, setPlan] = useState<CommandPlan>({ tick: game.tick ?? 0, unit_actions: {} })
  const [movementGoals, setMovementGoals] = useState<MovementGoals>(() => readMovementGoals(localStorage.getItem(movementStorageKey)))
  const planRef = useRef(plan); const tickRef = useRef(game.tick); const submitQueueRef = useRef<Promise<void>>(Promise.resolve()); const movementGoalsRef = useRef(movementGoals); const autoMovementTickRef = useRef<number | null>(null)
  const respawning = game.state?.status === 'RESPAWNING'
  const replaceMovementGoals = useCallback((next: MovementGoals) => { movementGoalsRef.current = next; setMovementGoals(next) }, [])
  const removeMovementGoal = useCallback((objectId: string) => {
    if (!movementGoalsRef.current[objectId]) return
    const next = { ...movementGoalsRef.current }; delete next[objectId]; replaceMovementGoals(next)
  }, [replaceMovementGoals])
  useEffect(() => { localStorage.setItem(movementStorageKey, JSON.stringify(movementGoals)) }, [movementGoals, movementStorageKey])
  // Layer-toggle persistence lives in effects, not inside setState updaters:
  // updaters must stay pure (StrictMode invokes them twice in development).
  useEffect(() => { localStorage.setItem(enemyMemoryStorageKey, String(enemyMemoryVisible)) }, [enemyMemoryStorageKey, enemyMemoryVisible])
  useEffect(() => { localStorage.setItem(beaconIndicatorStorageKey, String(beaconIndicatorVisible)) }, [beaconIndicatorStorageKey, beaconIndicatorVisible])
  useEffect(() => { localStorage.setItem(coreIndicatorStorageKey, String(coreIndicatorVisible)) }, [coreIndicatorStorageKey, coreIndicatorVisible])
  useEffect(() => { localStorage.setItem(unitRoutesStorageKey, String(unitRoutesVisible)) }, [unitRoutesStorageKey, unitRoutesVisible])
  useEffect(() => { localStorage.setItem(obstaclesStorageKey, String(obstaclesVisible)) }, [obstaclesStorageKey, obstaclesVisible])
  useEffect(() => { localStorage.setItem(enemyFilterStorageKey, JSON.stringify(enemyMemoryFilters)) }, [enemyFilterStorageKey, enemyMemoryFilters])
  useEffect(() => { if (game.tick) { const nextPlan = { tick: game.tick, unit_actions: {} }; tickRef.current = game.tick; planRef.current = nextPlan; autoMovementTickRef.current = null; setPlan(nextPlan); setTargetMode(null); setMoveSelecting(false); setMovementError(null) } }, [game.tick])
  useEffect(() => {
    const authoritative = game.receipts.MANUAL
    if (!game.tick || authoritative?.tick !== game.tick) return
    planRef.current = authoritative.plan
    setPlan(authoritative.plan)
  }, [game.receipts.MANUAL, game.tick])
  useEffect(() => { if (respawning) { setSelectedId(null); setTargetMode(null); setMoveSelecting(false); setMovementError(null); setAnchor(null); setCoordPick(null); setStrategyPick(null); setFleetOpen(false); if (Object.keys(movementGoalsRef.current).length) replaceMovementGoals({}) } }, [replaceMovementGoals, respawning])
  useEffect(() => {
    if (!game.state) return
    if (!respawning) {
      setCoreDestroyer(null); setCoreSelfDestructed(false)
      sessionStorage.removeItem(destroyerStorageKey); sessionStorage.removeItem(selfDestructStorageKey)
      return
    }
    const destruction = coreDestructionFromEvents(game.state.events)
    if (!destruction) return
    setCoreDestroyer(destruction.destroyedBy)
    setCoreSelfDestructed(destruction.selfDestructed)
    if (destruction.destroyedBy) sessionStorage.setItem(destroyerStorageKey, destruction.destroyedBy)
    else sessionStorage.removeItem(destroyerStorageKey)
    if (destruction.selfDestructed) sessionStorage.setItem(selfDestructStorageKey, 'true')
    else sessionStorage.removeItem(selfDestructStorageKey)
  }, [destroyerStorageKey, game.state, respawning, selfDestructStorageKey])
  const commitManualPlan = useCallback((nextPlan: CommandPlan) => {
    planRef.current = nextPlan; setPlan(nextPlan)
    submitQueueRef.current = submitQueueRef.current.then(async () => {
      if (nextPlan.tick !== tickRef.current) return
      try { await submitGamePlan(nextPlan) } catch { /* surfaced by the game stream */ }
    })
  }, [submitGamePlan])
  // One navigation snapshot per tick: the autonomous pass, the rendering pass
  // and the map-click handler all reuse it instead of rebuilding the obstacle
  // and occupancy indexes for every A* call.
  const navigationContext = useMemo(() => game.state ? createNavigationContextCached(game.state, game.explored) : null, [game.explored, game.state])
  // Routes are a by-product of the autonomous-movement pass (it already ran A*
  // for every goal), so the canvas never path-finds a second time per tick.
  const [autonomousRoutes, setAutonomousRoutes] = useState<MovementRoute[]>([])
  const movementRoutes = useMemo(() => {
    if (!autonomousRoutes.length) return EMPTY_MOVEMENT_ROUTES
    const activeGoals = new Set(Object.keys(movementGoals))
    const active = autonomousRoutes.filter((route) => activeGoals.has(route.objectId))
    return active.length === autonomousRoutes.length ? autonomousRoutes : active
  }, [autonomousRoutes, movementGoals])
  useEffect(() => {
    if (!game.tick || !game.state || game.phase !== 'open' || respawning || autoMovementTickRef.current === game.tick) return
    autoMovementTickRef.current = game.tick
    const currentPlan = planRef.current.tick === game.tick ? planRef.current : { tick: game.tick, unit_actions: {} }
    const result = applyAutonomousMovement(game.state, game.explored, movementGoalsRef.current, currentPlan, navigationContext ?? undefined)
    setAutonomousRoutes((current) => sameMovementRoutes(current, result.routes) ? current : result.routes)
    const stale = new Set([...result.completed, ...result.removed])
    if (stale.size) replaceMovementGoals(Object.fromEntries(Object.entries(movementGoalsRef.current).filter(([objectId]) => !stale.has(objectId))))
    if (result.changed) commitManualPlan(result.plan)
  }, [commitManualPlan, game.explored, game.phase, game.state, game.tick, navigationContext, replaceMovementGoals, respawning])
  const selected = useMemo(() => game.state?.objects.find((object) => object.id === selectedId) ?? null, [game.state, selectedId])
  const attackOptions = useMemo(() => {
    if (!selected || !targetMode) return []
    if (targetMode === 'SWEEP') return vanguardAttackOptions(selected)
    return game.state ? rangerAttackOptions(game.state, selected) : []
  }, [game.state, selected, targetMode])
  const attackPositions = useMemo(() => attackOptions.map((option) => option.position), [attackOptions])
  const effective = useMemo(() => mergeCommandPlans(game.tick ?? 0, game.receipts, plan), [game.receipts, game.tick, plan])
  const actionAvailability = useMemo(() => game.state && selected ? getActionAvailability(game.state, selected, effective.plan) : null, [effective.plan, game.state, selected])
  const moveArrows = useMemo(() => game.state ? plannedMoveArrows(game.state, effective.plan, movementRoutes, effective) : [], [effective, game.state, movementRoutes])
  const plannedRouteDestinations = useMemo(() => {
    const routesByObject = new Map(movementRoutes.map((route) => [route.objectId, route] as const))
    return Object.entries(movementGoals).map(([objectId, position]) => ({ objectId, position, blocked: routesByObject.get(objectId)?.blocked ?? true }))
  }, [movementGoals, movementRoutes])
  const routeDestinations = useMemo(() => {
    const attackDestinations = targetMode && selected?.id
      ? attackOptions.map((option) => ({ objectId: selected.id!, position: option.position, blocked: false, selectable: true, immediate: true, hostile: true }))
      : []
    if (!moveSelecting || !game.state || !selected?.id) return [...plannedRouteDestinations, ...attackDestinations]
    const immediate = new Set(moveTargets(game.state, selected, effective.plan).map(positionKey))
    const selectable = reachableMovementDestinations(game.state, game.explored, selected, effective.plan, navigationContext ?? undefined).map((position) => ({ objectId: selected.id!, position, blocked: false, selectable: true, immediate: immediate.has(positionKey(position)) }))
    return [...plannedRouteDestinations.filter((destination) => destination.objectId !== selected.id), ...selectable]
  }, [attackOptions, effective.plan, game.explored, game.state, moveSelecting, navigationContext, plannedRouteDestinations, selected, targetMode])
  const sweepMarkers = useMemo(() => game.state ? plannedSweepMarkers(game.state, effective.plan, effective.unitSources) : [], [effective.plan, effective.unitSources, game.state])
  const shotMarkers = useMemo(() => game.state ? plannedShotMarkers(game.state, effective.plan, effective.unitSources) : [], [effective.plan, effective.unitSources, game.state])
  const targetableIds = useMemo(() => {
    const positions = new Set(attackPositions.map(positionKey))
    return new Set((game.state?.objects ?? []).flatMap((object) => object.id && object.controlled === false && object.position && positions.has(positionKey(object.position)) ? [object.id] : []))
  }, [attackPositions, game.state])
  const select = useCallback((object: WorldObject | null) => { setSelectedId(object?.id ?? null); setTargetMode(null); setMoveSelecting(false); setMovementError(null) }, [])
  const selectFromAssetList = useCallback((object: WorldObject) => {
    select(object)
    setFleetOpen(false)
    if (!object.position) return
    setCenterPosition(object.position)
    setCenterRequest((value) => value + 1)
  }, [select])
  // Same jump behavior for enemy sightings: land the camera on the threat so
  // the operator can immediately order units into combat.
  const jumpToEnemy = useCallback((enemy: WorldObject) => {
    select(enemy)
    if (!enemy.position) return
    setCenterPosition(enemy.position)
    setCenterRequest((value) => value + 1)
  }, [select])
  // Remembered enemies have no live object to select; just center the camera.
  // Battle-log coordinates jump the same way.
  const jumpToPosition = useCallback((position: Position) => {
    setCenterPosition(position)
    setCenterRequest((value) => value + 1)
  }, [])
  const openFleet = useCallback(() => setFleetOpen(true), [])
  const closeFleet = useCallback(() => setFleetOpen(false), [])
  const toggleEnemyMemory = useCallback(() => setEnemyMemoryVisible((visible) => !visible), [])
  const toggleBeaconIndicator = useCallback(() => setBeaconIndicatorVisible((visible) => !visible), [])
  const toggleCoreIndicator = useCallback(() => setCoreIndicatorVisible((visible) => !visible), [])
  const toggleUnitRoutes = useCallback(() => setUnitRoutesVisible((visible) => !visible), [])
  const toggleObstacles = useCallback(() => setObstaclesVisible((visible) => !visible), [])
  const toggleEnemyMemoryFilter = useCallback((type: EnemySightingType) => setEnemyMemoryFilters((current) => current.includes(type) ? current.filter((item) => item !== type) : [...current, type]), [])
  // Memory markers skip cells where a live enemy now stands, so a re-spotted
  // threat never shows twice (dim marker under the bright unit sprite).
  // Unknown (ENEMY) entries bypass the per-type filters.
  const memoryEnemies = useMemo(() => {
    if (!enemyMemoryVisible) return []
    const filters = new Set<EnemySightingType>(enemyMemoryFilters)
    const live = new Set((game.state?.objects ?? []).filter((object) => object.controlled === false && object.position).map((object) => positionKey(object.position!)))
    return enemyMemory.filter((sighting) => (sighting.type === 'ENEMY' || filters.has(sighting.type)) && !live.has(positionKey(sighting.position)))
  }, [enemyMemory, enemyMemoryFilters, enemyMemoryVisible, game.state])
  // Top-left strip keeps at most MEMORY_SIGHTINGS_MAX entries, most recently
  // seen first; the cap is relative to the already-filtered set, so a
  // single-type filter and a multi-type filter each total 21 at most.
  // The map markers below keep the full filtered set.
  const memoryEnemyList = useMemo(() => memoryEnemies
    .slice()
    .sort((left, right) => right.tick - left.tick)
    .slice(0, MEMORY_SIGHTINGS_MAX), [memoryEnemies])
  const memoryFilterSet = useMemo(() => new Set(enemyMemoryFilters), [enemyMemoryFilters])
  const displayedUnitRoutes = useMemo(() => unitRoutesVisible ? unitRoutes : EMPTY_UNIT_ROUTES, [unitRoutes, unitRoutesVisible])
  const setUnitAction = useCallback((id: string, action: UnitAction | null) => { const current = planRef.current; const unit_actions = { ...current.unit_actions }; if (action) unit_actions[id] = action; else delete unit_actions[id]; commitManualPlan({ ...current, unit_actions }) }, [commitManualPlan])
  const setCoreAction = useCallback((action: CoreAction | null) => { const current = planRef.current; if (action) { commitManualPlan({ ...current, core_action: action }); return } const next = { ...current }; delete next.core_action; commitManualPlan(next) }, [commitManualPlan])
  const unitAction = useCallback((id: string, action: UnitAction | null) => {
    removeMovementGoal(id)
    if (!game.state) return
    commitManualPlan(prepareManualUnitActionPlan(game.state, game.receipts, planRef.current, id, action))
  }, [commitManualPlan, game.receipts, game.state, removeMovementGoal])
  const coreAction = useCallback((action: CoreAction | null) => { const coreId = game.state?.objects.find((object) => object.kind === 'CORE' && object.controlled)?.id; if (coreId) removeMovementGoal(coreId); setCoreAction(action) }, [game.state, removeMovementGoal, setCoreAction])
  const chooseAttackPosition = useCallback((position: Position) => {
    if (!selected?.id || !selected.position) return
    if (targetMode === 'SWEEP' && selected.unit_type === 'VANGUARD') {
      const direction = directionTo(selected.position, position); if (!direction) return
      unitAction(selected.id, { type: 'SWEEP', direction }); select(null); return
    }
    if (targetMode !== 'SHOOT' || selected.unit_type !== 'RANGER') return
    const option = attackOptions.find((candidate) => positionKey(candidate.position) === positionKey(position))
    if (!option) return
    unitAction(selected.id, { type: 'SHOOT', expected_cell: position }); select(null)
  }, [attackOptions, select, selected, targetMode, unitAction])
  const chooseTarget = useCallback((target: WorldObject) => { if (target.position) chooseAttackPosition(target.position) }, [chooseAttackPosition])
  const startCoordPick = useCallback((xField: string, yField: string) => { setTargetMode(null); setMoveSelecting(false); setSelectedId(null); setFleetOpen(false); setCoordPick({ xField, yField }) }, [])
  const completeCoordPick = useCallback((position: Position) => {
    if (!coordPick) return
    const { xField, yField } = coordPick
    setCoordPick(null)
    // Two back-to-back updates: the hook stacks them on the same snapshot.
    updateConfig(xField, position[0])
    updateConfig(yField, position[1])
  }, [coordPick, updateConfig])
  const startStrategyCoordPick = useCallback((xField: string, yField: string) => { setStrategyConfigOpen(false); setStrategyPick({ xField, yField }) }, [])
  const completeStrategyPick = useCallback((position: Position) => {
    if (!strategyPick) return
    const { xField, yField } = strategyPick
    setStrategyPick(null)
    // X and Y go out in one partial patch so the pair is never half-applied;
    // reopening the dialog remounts it, reloading /api/config with the new
    // coordinates.
    void saveStrategyValues({ [xField]: position[0], [yField]: position[1] }).then(() => setStrategyConfigOpen(true))
  }, [strategyPick])
  const cancelStrategyPick = useCallback(() => { setStrategyPick(null); setStrategyConfigOpen(true) }, [])
  // While picking, mark the currently configured target so the operator sees
  // what the new point will replace.
  const coordPickHighlight = useMemo<Position[]>(() => {
    if (!coordPick) return []
    const x = Number(teamConfig[coordPick.xField]); const y = Number(teamConfig[coordPick.yField])
    return Number.isFinite(x) && Number.isFinite(y) ? [[x, y]] : []
  }, [coordPick, teamConfig])
  const startWaypointPick = useCallback((object: WorldObject) => {
    const name = unitDashboardName(object, unitNames)
    if (!name) return
    setTargetMode(null); setMoveSelecting(false)
    setWaypointPick({ name, mode: waypoints[name]?.mode ?? 'attack' })
  }, [unitNames, waypoints])
  const completeWaypointPick = useCallback((position: Position) => {
    if (!waypointPick) return
    const { name, mode } = waypointPick
    setWaypointPick(null)
    void addUnitWaypoint(name, position[0], position[1], mode).then((ok) => { if (ok) refreshWaypoints() })
  }, [refreshWaypoints, waypointPick])
  const changeWaypoints = useCallback((name: string, index?: number) => void removeUnitWaypoint(name, index).then((ok) => { if (ok) refreshWaypoints() }), [refreshWaypoints])
  const waypointPickHighlight = useMemo<Position[]>(() => waypointPick ? waypoints[waypointPick.name]?.queue ?? [] : [], [waypointPick, waypoints])
  const startSquadTargetPick = useCallback((squad: SquadKey) => { setTargetMode(null); setMoveSelecting(false); setSelectedId(null); setFleetOpen(false); setSquadTargetPick(squad) }, [])
  const completeSquadTargetPick = useCallback((position: Position) => {
    if (!squadTargetPick) return
    const squad = squadTargetPick
    setSquadTargetPick(null)
    void addSquadTarget(squad, position[0], position[1]).then((ok) => { if (ok) refreshSquadTargets() })
  }, [refreshSquadTargets, squadTargetPick])
  const changeSquadTargets = useCallback((squad: SquadKey, index?: number) => void (index === undefined ? clearSquadTargets(squad) : removeSquadTarget(squad, index)).then((ok) => { if (ok) refreshSquadTargets() }), [refreshSquadTargets])
  const squadTargetPickHighlight = useMemo<Position[]>(() => squadTargetPick ? squadTargets[squadTargetPick] ?? [] : [], [squadTargetPick, squadTargets])
  const highlightPositions = useMemo(() => [...coordPickHighlight, ...waypointPickHighlight, ...squadTargetPickHighlight], [coordPickHighlight, squadTargetPickHighlight, waypointPickHighlight])
  const handleCoordPick = useCallback((position: Position) => {
    if (squadTargetPick) completeSquadTargetPick(position)
    else if (waypointPick) completeWaypointPick(position)
    else if (strategyPick) completeStrategyPick(position)
    else completeCoordPick(position)
  }, [completeCoordPick, completeSquadTargetPick, completeStrategyPick, completeWaypointPick, squadTargetPick, strategyPick, waypointPick])
  // Esc backs out of whichever map-picking mode is active (shoot/sweep
  // targeting, move destination, coordinate/waypoint/squad/strategy picks)
  // without reaching for the hint banner's cancel button.
  useEffect(() => {
    if (!targetMode && !moveSelecting && !coordPick && !waypointPick && !squadTargetPick && !strategyPick) return
    const cancelOnEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      setTargetMode(null); setMoveSelecting(false); setMovementError(null)
      setCoordPick(null); setWaypointPick(null); setSquadTargetPick(null)
      if (strategyPick) { setStrategyPick(null); setStrategyConfigOpen(true) }
    }
    window.addEventListener('keydown', cancelOnEscape)
    return () => window.removeEventListener('keydown', cancelOnEscape)
  }, [coordPick, moveSelecting, squadTargetPick, strategyPick, targetMode, waypointPick])
  useEffect(() => {
    if (!fleetOpen) return
    const closeOnEscape = (event: KeyboardEvent) => { if (event.key === 'Escape') setFleetOpen(false) }
    window.addEventListener('keydown', closeOnEscape)
    return () => window.removeEventListener('keydown', closeOnEscape)
  }, [fleetOpen])
  const chooseMoveDestination = useCallback((target: Position) => {
    if (!game.state || !selected?.id || !selected.position) return
    if (selected.position[0] === target[0] && selected.position[1] === target[1]) { removeMovementGoal(selected.id); if (selected.kind === 'CORE') setCoreAction(null); else setUnitAction(selected.id, null); select(null); return }
    const path = findMovementPath(game.state, game.explored, selected, target, planRef.current, navigationContext ?? undefined)
    if (!path.path || path.path.length < 2) { setMovementError(path.reason ?? 'NO_ROUTE'); return }
    const nextGoals = { ...movementGoalsRef.current, [selected.id]: target }
    const result = applyAutonomousMovement(game.state, game.explored, nextGoals, planRef.current, navigationContext ?? undefined)
    replaceMovementGoals(nextGoals); setMovementError(null); autoMovementTickRef.current = game.tick
    setAutonomousRoutes((current) => sameMovementRoutes(current, result.routes) ? current : result.routes)
    if (result.changed) commitManualPlan(result.plan)
    select(null)
  }, [commitManualPlan, game.explored, game.state, game.tick, navigationContext, removeMovementGoal, replaceMovementGoals, select, selected, setCoreAction, setUnitAction])
  const cancelMovementGoal = useCallback((object: WorldObject) => { if (!object.id) return; removeMovementGoal(object.id); if (object.kind === 'CORE') setCoreAction(null); else setUnitAction(object.id, null); select(null) }, [removeMovementGoal, select, setCoreAction, setUnitAction])
  const selectedWaypointName = selected?.kind === 'UNIT' ? unitDashboardName(selected, unitNames) : undefined
  const selectedWaypointEntry = selectedWaypointName ? waypoints[selectedWaypointName] : undefined
  const selectedHoldActive = selectedWaypointName ? holds.has(selectedWaypointName) : false
  const toggleSelectedHold = useCallback(() => {
    if (!selectedWaypointName) return
    const next = !selectedHoldActive
    const request = next ? setUnitHold : clearUnitHold
    void request(selectedWaypointName).then((ok) => { if (ok) refreshHolds() })
  }, [refreshHolds, selectedHoldActive, selectedWaypointName])
  const centerOnBeacon = useCallback(() => { if (!game.state) return; setCenterPosition(game.state.champion_beacon.position); setCenterRequest((value) => value + 1) }, [game.state])
  const centerOnCore = useCallback(() => { setCenterPosition(null); setCenterRequest((value) => value + 1) }, [])
  const requestZoom = useCallback((direction: number) => setZoomRequest((value) => direction * (Math.abs(value) + 1)), [])
  const closeUnitDialog = useCallback(() => select(null), [select])
  const startShootTargeting = useCallback(() => { setMoveSelecting(false); setTargetMode('SHOOT') }, [])
  const startSweepTargeting = useCallback(() => { setMoveSelecting(false); setTargetMode('SWEEP') }, [])
  const startMoveTargeting = useCallback(() => { setTargetMode(null); setMovementError(null); setMoveSelecting(true) }, [])
  const openStrategyConfig = useCallback(() => setStrategyConfigOpen(true), [])
  const closeStrategyConfig = useCallback(() => setStrategyConfigOpen(false), [])
  const openGuerrillaTracks = useCallback(() => { setGuerrillaTracksDate(null); setGuerrillaTracksOpen(true) }, [])
  const closeGuerrillaTracks = useCallback(() => setGuerrillaTracksOpen(false), [])
  // The arena is the only route that paints the world background, so its
  // preload lives here instead of a global <link> in index.html that /login and
  // /leaderboard fetched for nothing.
  useEffect(() => {
    const link = document.createElement('link')
    link.rel = 'preload'; link.as = 'image'; link.type = 'image/webp'; link.href = WORLD_BACKGROUND_PATH
    link.dataset.arenaBackground = 'true'
    document.head.appendChild(link)
    return () => { link.remove() }
  }, [])
  if (!game.state) return <div className="grid h-dvh place-items-center"><div className="text-center"><div className="mx-auto mb-4 size-2 animate-pulse rounded-full bg-cyan-signal shadow-[0_0_14px_rgba(69,145,197,.45)]" /><p className="font-mono text-xs tracking-[.2em] text-zinc-500">{t(`game.${game.phase}`)}</p>{game.error && <p role="alert" className="mt-3 text-xs text-coral-hostile">{describeError(game.error)}</p>}</div></div>
  const fleetProps = { state: game.state, objects: game.state.objects, selectedId, onSelect: selectFromAssetList, unitNames, teamRoster, onAssignTeam: demo ? undefined : assignTeam, teamConfig, onUpdateConfig: demo ? undefined : updateConfig, onPickCoords: demo ? undefined : startCoordPick, pickingCoordsField: coordPick?.xField ?? null, squadTargets, onPickSquadTarget: demo ? undefined : startSquadTargetPick, onRemoveSquadTarget: demo ? undefined : changeSquadTargets, onClearSquadTargets: demo ? undefined : changeSquadTargets, pickingSquadTarget: squadTargetPick }
  return <div className="grid h-dvh min-h-[420px] grid-cols-1 overflow-hidden lg:grid-cols-[260px_1fr]">
    <AssetList {...fleetProps} />
    {fleetOpen && !respawning && <div className="fixed inset-0 z-40 lg:hidden" role="dialog" aria-modal="true" aria-label={t('game.objects')}>
      <button type="button" aria-label={t('common.close')} onClick={closeFleet} className="absolute inset-0 bg-black/65" />
      <div className="absolute inset-y-0 left-0 w-[min(300px,86vw)] shadow-[12px_0_40px_rgba(0,0,0,.5)]">
        <AssetList {...fleetProps} drawer onCloseDrawer={closeFleet} />
      </div>
    </div>}
    <section className="relative min-h-0 overflow-hidden">
      {!respawning && <GameHUD phase={game.phase} stateReceivedAt={game.stateReceivedAt} />}
      {!respawning && <div className="pointer-events-none absolute inset-x-3 top-16 z-20 flex flex-col gap-2 lg:contents">
        <EnemySightings state={game.state} onJump={jumpToEnemy} sightings={memoryEnemyList} onJumpTo={jumpToPosition} />
        <div className="flex w-full flex-col gap-2 lg:absolute lg:right-3 lg:top-16 lg:z-20 lg:w-[min(19rem,calc(100%-1.5rem))]">
          <BattleLogPanel tick={game.tick} enabled={!demo} onJump={jumpToPosition} />
          {game.tick && <PendingCommands tick={game.tick} state={game.state} receipts={game.receipts} unitNames={unitNames} />}
        </div>
      </div>}
      <WorldCanvas state={game.state} explored={game.explored} selectedId={selectedId} targeting={targetMode !== null} destinationSelecting={moveSelecting} attackPositions={attackPositions} targetableIds={targetableIds} routeDestinations={routeDestinations} moveArrows={moveArrows} sweepMarkers={sweepMarkers} shotMarkers={shotMarkers} centerPosition={centerPosition} centerRequest={centerRequest} zoomRequest={zoomRequest} onSelect={select} onTarget={chooseTarget} onAttackPosition={chooseAttackPosition} onMoveDestination={chooseMoveDestination} onCenterBeacon={centerOnBeacon} onCenterCore={centerOnCore} beaconIndicatorVisible={beaconIndicatorVisible} coreIndicatorVisible={coreIndicatorVisible} onAnchorChange={setAnchor} coordPicking={coordPick !== null || waypointPick !== null || strategyPick !== null || squadTargetPick !== null} onCoordPick={handleCoordPick} highlightPositions={highlightPositions} memoryEnemies={memoryEnemies} unitRoutes={displayedUnitRoutes} obstaclesVisible={obstaclesVisible} />
      {!respawning && <ResourceActivity events={game.state.events} />}
      {respawning && <RespawnOverlay destroyedBy={coreDestroyer} selfDestructed={coreSelfDestructed} />}
      {!respawning && selected?.controlled && anchor && actionAvailability && !targetMode && !moveSelecting && !waypointPick && <UnitActionDialog anchor={anchor} selected={selected} plan={plan} movementGoal={selected.id ? movementGoals[selected.id] : undefined} unitNames={unitNames} phase={game.phase} resources={game.state.resources} population={game.state.population} availability={actionAvailability} waypointName={selectedWaypointName} waypointQueue={selectedWaypointEntry?.queue} waypointMode={selectedWaypointEntry?.mode} onPickWaypoint={demo ? undefined : () => startWaypointPick(selected)} onRemoveWaypoint={demo || !selectedWaypointName ? undefined : (index) => changeWaypoints(selectedWaypointName, index)} onClearWaypoints={demo || !selectedWaypointName ? undefined : () => changeWaypoints(selectedWaypointName)} holdActive={selectedHoldActive} onToggleHold={demo || !selectedWaypointName ? undefined : toggleSelectedHold} onClose={closeUnitDialog} onTargeting={startShootTargeting} onSweepTargeting={startSweepTargeting} onMoveTargeting={startMoveTargeting} onCancelMovementGoal={() => cancelMovementGoal(selected)} onUnitAction={unitAction} onCoreAction={coreAction} />}
      {targetMode && <div className="panel absolute left-1/2 top-28 z-30 flex -translate-x-1/2 items-center gap-2 rounded-full pl-4 pr-1.5 text-xs text-coral-hostile">{targetMode === 'SWEEP' ? <Sword size={15} /> : <Crosshair size={15} />}<span>{t(targetMode === 'SWEEP' ? 'game.sweepHint' : 'game.targetHint')}</span><button onClick={() => setTargetMode(null)} className="focus-ring ml-1 min-h-11 rounded-full px-3 text-zinc-400 hover:bg-white/5 hover:text-white">{t('common.cancel')}</button></div>}
      {moveSelecting && <div className={`panel absolute left-1/2 top-28 z-30 flex -translate-x-1/2 items-center gap-2 rounded-full pl-4 pr-1.5 text-xs ${movementError ? 'text-coral-hostile' : 'text-cyan-signal'}`}><Move size={15} /><span>{t(movementError === 'UNKNOWN_DESTINATION' ? 'game.routeUnknown' : movementError ? 'game.routeBlocked' : 'game.moveHint')}</span><button onClick={() => { setMoveSelecting(false); setMovementError(null) }} className="focus-ring ml-1 min-h-11 rounded-full px-3 text-zinc-400 hover:bg-white/5 hover:text-white">{t('common.cancel')}</button></div>}
      {coordPick && <div className="panel absolute left-1/2 top-28 z-30 flex -translate-x-1/2 items-center gap-2 rounded-full pl-4 pr-1.5 text-xs text-cyan-signal"><Crosshair size={15} /><span>{t('game.coordPickHint')}</span><button onClick={() => setCoordPick(null)} className="focus-ring ml-1 min-h-11 rounded-full px-3 text-zinc-400 hover:bg-white/5 hover:text-white">{t('common.cancel')}</button></div>}
      {strategyPick && <div className="panel absolute left-1/2 top-28 z-30 flex -translate-x-1/2 items-center gap-2 rounded-full pl-4 pr-1.5 text-xs text-cyan-signal"><Crosshair size={15} /><span>{t('game.coordPickHint')}</span><button onClick={cancelStrategyPick} className="focus-ring ml-1 min-h-11 rounded-full px-3 text-zinc-400 hover:bg-white/5 hover:text-white">{t('common.cancel')}</button></div>}
      {waypointPick && <div className="panel absolute left-1/2 top-28 z-30 flex -translate-x-1/2 items-center gap-2 rounded-full pl-4 pr-1.5 text-xs text-cyan-signal"><Crosshair size={15} /><span>{t('game.waypointPickHint', { name: waypointPick.name })}</span><button onClick={() => setWaypointPick(null)} className="focus-ring ml-1 min-h-11 rounded-full px-3 text-zinc-400 hover:bg-white/5 hover:text-white">{t('common.cancel')}</button></div>}
      {squadTargetPick && <div className="panel absolute left-1/2 top-28 z-30 flex -translate-x-1/2 items-center gap-2 rounded-full pl-4 pr-1.5 text-xs text-cyan-signal"><Crosshair size={15} /><span>{t('game.squadTargetPickHint', { squad: t(`game.teamSettings.${squadTargetPick === 'attack' ? 'attackQueueName' : 'kiteQueueName'}`) })}</span><button onClick={() => setSquadTargetPick(null)} className="focus-ring ml-1 min-h-11 rounded-full px-3 text-zinc-400 hover:bg-white/5 hover:text-white">{t('common.cancel')}</button></div>}
      {!respawning && <MapControls onCenter={centerOnCore} onZoom={requestZoom} beaconIndicatorVisible={beaconIndicatorVisible} onToggleBeaconIndicator={toggleBeaconIndicator} coreIndicatorVisible={coreIndicatorVisible} onToggleCoreIndicator={toggleCoreIndicator} memoryVisible={enemyMemoryVisible} onToggleMemory={toggleEnemyMemory} routesVisible={unitRoutesVisible} onToggleRoutes={demo ? undefined : toggleUnitRoutes} obstaclesVisible={obstaclesVisible} onToggleObstacles={toggleObstacles} memoryFilters={memoryFilterSet} onToggleMemoryFilter={toggleEnemyMemoryFilter} />}
      {!respawning && <button type="button" onClick={openFleet} aria-label={t('game.openFleet')} title={t('game.openFleet')} className="focus-ring panel absolute bottom-4 left-4 z-20 grid size-11 place-items-center rounded-gold text-zinc-300 transition-colors hover:bg-white/[.06] hover:text-zinc-100 lg:hidden"><Users size={18} /></button>}
      {!demo && !respawning && <button ref={guerrillaTracksButtonRef} type="button" onClick={openGuerrillaTracks} aria-label={t('game.openGuerrillaTracks')} title={t('game.openGuerrillaTracks')} className="focus-ring panel absolute bottom-4 right-16 z-20 grid size-11 place-items-center rounded-gold text-zinc-400 transition-colors hover:bg-white/[.06] hover:text-zinc-100"><Footprints size={18} /></button>}
      {!demo && !respawning && <button ref={strategyConfigButtonRef} type="button" onClick={openStrategyConfig} aria-label={t('game.openStrategyConfig')} title={t('game.openStrategyConfig')} className="focus-ring panel absolute bottom-4 right-4 z-20 grid size-11 place-items-center rounded-gold text-zinc-400 transition-colors hover:bg-white/[.06] hover:text-zinc-100"><Settings size={18} /></button>}
      {!demo && strategyConfigOpen && <StrategyConfigDialog returnFocusRef={strategyConfigButtonRef} onClose={closeStrategyConfig} onPickCoords={startStrategyCoordPick} />}
      {!demo && guerrillaTracksOpen && <GuerrillaTracksDialog returnFocusRef={guerrillaTracksButtonRef} onClose={closeGuerrillaTracks} date={guerrillaTracks.date} dates={guerrillaTracks.dates} units={guerrillaTracks.units} onSelectDate={setGuerrillaTracksDate} />}
      {game.error && <div role="alert" className="panel absolute bottom-4 right-4 z-30 max-w-[min(24rem,calc(100%-2rem))] px-4 py-3 text-xs leading-5 text-coral-hostile">{describeError(game.error)}</div>}
    </section>
  </div>
}