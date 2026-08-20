import { Crosshair, Move, Sword } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { AssetList } from '../components/game/AssetList'
import { EnemySightings } from '../components/game/EnemySightings'
import { GameHUD } from '../components/game/GameHUD'
import { ENEMY_FILTER_TYPES, MapControls } from '../components/game/MapControls'
import { PendingCommands } from '../components/game/PendingCommands'
import { ResourceActivity } from '../components/game/ResourceActivity'
import { RespawnOverlay } from '../components/game/RespawnOverlay'
import { WorldCanvas } from '../components/game/WorldCanvas'
import { UnitActionDialog, type MapAnchor } from '../components/game/UnitActionDialog'
import { useGameStream } from '../hooks/useGameStream'
import { useEnemyMemory } from '../hooks/useEnemyMemory'
import { useUnitNames } from '../hooks/useUnitNames'
import { useUnitTeams } from '../hooks/useUnitTeams'
import { useWaypoints } from '../hooks/useWaypoints'
import { useAuth } from '../context/AuthContext'
import type { EnemySightingType } from '../lib/enemyMemory'
import { addUnitWaypoint, removeUnitWaypoint, type WaypointMode } from '../lib/waypoints'
import { unitDashboardName } from '../lib/unitNames'
import { plannedShotMarkers, plannedSweepMarkers, rangerAttackOptions, vanguardAttackOptions } from '../lib/combatPreview'
import { directionTo, moveTargets, plannedMoveArrows } from '../lib/movementPreview'
import { getErrorMessage } from '../lib/errorMessage'
import { getActionAvailability } from '../lib/actionAvailability'
import { coreDestructionFromEvents } from '../lib/destruction'
import { applyAutonomousMovement, buildMovementRoutes, findMovementPath, reachableMovementDestinations, readMovementGoals, type MovementGoals, type PathFailure } from '../lib/pathfinding'
import { mergeCommandPlans, prepareManualUnitActionPlan } from '../lib/commandPlans'
import type { CommandPlan, CoreAction, Position, UnitAction, WorldObject } from '../lib/types'
import { positionKey } from '../lib/visibility'

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
  const submitGamePlan = game.submit
  const movementStorageKey = `arena-hero.movement-goals.${demo ? 'demo' : user?.username ?? 'anonymous'}`
  const [selectedId, setSelectedId] = useState<string | null>(null); const [targetMode, setTargetMode] = useState<'SHOOT' | 'SWEEP' | null>(null); const [moveSelecting, setMoveSelecting] = useState(false)
  // Squad target map picking (⌖ in the sidebar squad settings): while set,
  // the next map click fills that squad's target X/Y via /api/teams.
  const [coordPick, setCoordPick] = useState<{ xField: string; yField: string } | null>(null)
  // Manual-target map picking (选择目标点 in the unit dialog): the next map
  // click appends that point to the unit's waypoint queue.
  const [waypointPick, setWaypointPick] = useState<{ name: string; mode: WaypointMode } | null>(null)
  const [movementError, setMovementError] = useState<PathFailure | null>(null)
  const [anchor, setAnchor] = useState<MapAnchor | null>(null)
  const destroyerStorageKey = `arena-hero.core-destroyer.${demo ? 'demo' : user?.username ?? 'anonymous'}`
  const selfDestructStorageKey = `arena-hero.core-self-destructed.${demo ? 'demo' : user?.username ?? 'anonymous'}`
  const enemyMemoryStorageKey = `arena-hero.enemy-memory-visible.${demo ? 'demo' : user?.username ?? 'anonymous'}`
  const enemyFilterStorageKey = `arena-hero.enemy-memory-filters.${demo ? 'demo' : user?.username ?? 'anonymous'}`
  const beaconIndicatorStorageKey = `arena-hero.beacon-indicator-visible.${demo ? 'demo' : user?.username ?? 'anonymous'}`
  const coreIndicatorStorageKey = `arena-hero.core-indicator-visible.${demo ? 'demo' : user?.username ?? 'anonymous'}`
  const [coreDestroyer, setCoreDestroyer] = useState<string | null>(() => sessionStorage.getItem(destroyerStorageKey))
  const [coreSelfDestructed, setCoreSelfDestructed] = useState(() => sessionStorage.getItem(selfDestructStorageKey) === 'true')
  const [enemyMemoryVisible, setEnemyMemoryVisible] = useState(() => localStorage.getItem(enemyMemoryStorageKey) !== 'false')
  const [beaconIndicatorVisible, setBeaconIndicatorVisible] = useState(() => localStorage.getItem(beaconIndicatorStorageKey) !== 'false')
  const [coreIndicatorVisible, setCoreIndicatorVisible] = useState(() => localStorage.getItem(coreIndicatorStorageKey) !== 'false')
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
  useEffect(() => { if (game.tick) { const nextPlan = { tick: game.tick, unit_actions: {} }; tickRef.current = game.tick; planRef.current = nextPlan; autoMovementTickRef.current = null; setPlan(nextPlan); setTargetMode(null); setMoveSelecting(false); setMovementError(null) } }, [game.tick])
  useEffect(() => {
    const authoritative = game.receipts.MANUAL
    if (!game.tick || authoritative?.tick !== game.tick) return
    planRef.current = authoritative.plan
    setPlan(authoritative.plan)
  }, [game.receipts.MANUAL, game.tick])
  useEffect(() => { if (respawning) { setSelectedId(null); setTargetMode(null); setMoveSelecting(false); setMovementError(null); setAnchor(null); setCoordPick(null); if (Object.keys(movementGoalsRef.current).length) replaceMovementGoals({}) } }, [replaceMovementGoals, respawning])
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
  useEffect(() => {
    if (!game.tick || !game.state || game.phase !== 'open' || respawning || autoMovementTickRef.current === game.tick) return
    autoMovementTickRef.current = game.tick
    const currentPlan = planRef.current.tick === game.tick ? planRef.current : { tick: game.tick, unit_actions: {} }
    const result = applyAutonomousMovement(game.state, game.explored, movementGoalsRef.current, currentPlan)
    const stale = new Set([...result.completed, ...result.removed])
    if (stale.size) replaceMovementGoals(Object.fromEntries(Object.entries(movementGoalsRef.current).filter(([objectId]) => !stale.has(objectId))))
    if (result.changed) commitManualPlan(result.plan)
  }, [commitManualPlan, game.explored, game.phase, game.state, game.tick, replaceMovementGoals, respawning])
  const selected = useMemo(() => game.state?.objects.find((object) => object.id === selectedId) ?? null, [game.state, selectedId])
  const attackOptions = useMemo(() => {
    if (!selected || !targetMode) return []
    if (targetMode === 'SWEEP') return vanguardAttackOptions(selected)
    return game.state ? rangerAttackOptions(game.state, selected) : []
  }, [game.state, selected, targetMode])
  const attackPositions = useMemo(() => attackOptions.map((option) => option.position), [attackOptions])
  const effective = useMemo(() => mergeCommandPlans(game.tick ?? 0, game.receipts, plan), [game.receipts, game.tick, plan])
  const actionAvailability = useMemo(() => game.state && selected ? getActionAvailability(game.state, selected, effective.plan) : null, [effective.plan, game.state, selected])
  const movementRoutes = useMemo(() => game.state ? buildMovementRoutes(game.state, game.explored, movementGoals, effective.plan) : [], [effective.plan, game.explored, game.state, movementGoals])
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
    const selectable = reachableMovementDestinations(game.state, game.explored, selected, effective.plan).map((position) => ({ objectId: selected.id!, position, blocked: false, selectable: true, immediate: immediate.has(positionKey(position)) }))
    return [...plannedRouteDestinations.filter((destination) => destination.objectId !== selected.id), ...selectable]
  }, [attackOptions, effective.plan, game.explored, game.state, moveSelecting, plannedRouteDestinations, selected, targetMode])
  const sweepMarkers = useMemo(() => game.state ? plannedSweepMarkers(game.state, effective.plan, effective.unitSources) : [], [effective.plan, effective.unitSources, game.state])
  const shotMarkers = useMemo(() => game.state ? plannedShotMarkers(game.state, effective.plan, effective.unitSources) : [], [effective.plan, effective.unitSources, game.state])
	const targetableIds = useMemo(() => {
		const positions = new Set(attackPositions.map(positionKey))
		return new Set((game.state?.objects ?? []).flatMap((object) => object.id && object.controlled === false && object.position && positions.has(positionKey(object.position)) ? [object.id] : []))
	}, [attackPositions, game.state])
  const select = (object: WorldObject | null) => { setSelectedId(object?.id ?? null); setTargetMode(null); setMoveSelecting(false); setMovementError(null) }
  const selectFromAssetList = (object: WorldObject) => {
    select(object)
    if (!object.position) return
    setCenterPosition(object.position)
    setCenterRequest((value) => value + 1)
  }
  // Same jump behavior for enemy sightings: land the camera on the threat so
  // the operator can immediately order units into combat.
  const jumpToEnemy = (enemy: WorldObject) => {
    select(enemy)
    if (!enemy.position) return
    setCenterPosition(enemy.position)
    setCenterRequest((value) => value + 1)
  }
  // Remembered enemies have no live object to select; just center the camera.
  const jumpToMemoryEnemy = (position: Position) => {
    setCenterPosition(position)
    setCenterRequest((value) => value + 1)
  }
  const toggleEnemyMemory = () => setEnemyMemoryVisible((visible) => {
    const next = !visible
    localStorage.setItem(enemyMemoryStorageKey, String(next))
    return next
  })
  const toggleBeaconIndicator = () => setBeaconIndicatorVisible((visible) => {
    const next = !visible
    localStorage.setItem(beaconIndicatorStorageKey, String(next))
    return next
  })
  const toggleCoreIndicator = () => setCoreIndicatorVisible((visible) => {
    const next = !visible
    localStorage.setItem(coreIndicatorStorageKey, String(next))
    return next
  })
  const toggleEnemyMemoryFilter = (type: EnemySightingType) => setEnemyMemoryFilters((current) => {
    const next = current.includes(type) ? current.filter((item) => item !== type) : [...current, type]
    localStorage.setItem(enemyFilterStorageKey, JSON.stringify(next))
    return next
  })
  // Memory markers skip cells where a live enemy now stands, so a re-spotted
  // threat never shows twice (dim marker under the bright unit sprite).
  // Unknown (ENEMY) entries bypass the per-type filters.
  const memoryEnemies = useMemo(() => {
    if (!enemyMemoryVisible) return []
    const filters = new Set<EnemySightingType>(enemyMemoryFilters)
    const live = new Set((game.state?.objects ?? []).filter((object) => object.controlled === false && object.position).map((object) => positionKey(object.position!)))
    return enemyMemory.filter((sighting) => (sighting.type === 'ENEMY' || filters.has(sighting.type)) && !live.has(positionKey(sighting.position)))
  }, [enemyMemory, enemyMemoryFilters, enemyMemoryVisible, game.state])
  const setUnitAction = (id: string, action: UnitAction | null) => { const current = planRef.current; const unit_actions = { ...current.unit_actions }; if (action) unit_actions[id] = action; else delete unit_actions[id]; commitManualPlan({ ...current, unit_actions }) }
  const setCoreAction = (action: CoreAction | null) => { const current = planRef.current; if (action) { commitManualPlan({ ...current, core_action: action }); return } const next = { ...current }; delete next.core_action; commitManualPlan(next) }
  const unitAction = (id: string, action: UnitAction | null) => {
    removeMovementGoal(id)
    if (!game.state) return
    commitManualPlan(prepareManualUnitActionPlan(game.state, game.receipts, planRef.current, id, action))
  }
  const coreAction = (action: CoreAction | null) => { const coreId = game.state?.objects.find((object) => object.kind === 'CORE' && object.controlled)?.id; if (coreId) removeMovementGoal(coreId); setCoreAction(action) }
  const chooseAttackPosition = (position: Position) => {
    if (!selected?.id || !selected.position) return
    if (targetMode === 'SWEEP' && selected.unit_type === 'VANGUARD') {
      const direction = directionTo(selected.position, position); if (!direction) return
      unitAction(selected.id, { type: 'SWEEP', direction }); select(null); return
    }
		if (targetMode !== 'SHOOT' || selected.unit_type !== 'RANGER') return
		const option = attackOptions.find((candidate) => positionKey(candidate.position) === positionKey(position))
		if (!option) return
		unitAction(selected.id, { type: 'SHOOT', expected_cell: position }); select(null)
  }
  const chooseTarget = (target: WorldObject) => { if (target.position) chooseAttackPosition(target.position) }
  const startCoordPick = (xField: string, yField: string) => { setTargetMode(null); setMoveSelecting(false); setSelectedId(null); setCoordPick({ xField, yField }) }
  const completeCoordPick = (position: Position) => {
    if (!coordPick) return
    const { xField, yField } = coordPick
    setCoordPick(null)
    // Two back-to-back updates: the hook stacks them on the same snapshot.
    updateConfig(xField, position[0])
    updateConfig(yField, position[1])
  }
  // While picking, mark the currently configured target so the operator sees
  // what the new point will replace.
  const coordPickHighlight = useMemo<Position[]>(() => {
    if (!coordPick) return []
    const x = Number(teamConfig[coordPick.xField]); const y = Number(teamConfig[coordPick.yField])
    return Number.isFinite(x) && Number.isFinite(y) ? [[x, y]] : []
  }, [coordPick, teamConfig])
  const startWaypointPick = (object: WorldObject) => {
    const name = unitDashboardName(object, unitNames)
    if (!name) return
    setTargetMode(null); setMoveSelecting(false)
    setWaypointPick({ name, mode: waypoints[name]?.mode ?? 'attack' })
  }
  const completeWaypointPick = (position: Position) => {
    if (!waypointPick) return
    const { name, mode } = waypointPick
    setWaypointPick(null)
    void addUnitWaypoint(name, position[0], position[1], mode).then((ok) => { if (ok) refreshWaypoints() })
  }
  const changeWaypoints = (name: string, index?: number) => void removeUnitWaypoint(name, index).then((ok) => { if (ok) refreshWaypoints() })
  const waypointPickHighlight = useMemo<Position[]>(() => waypointPick ? waypoints[waypointPick.name]?.queue ?? [] : [], [waypointPick, waypoints])
  const chooseMoveDestination = (target: Position) => {
    if (!game.state || !selected?.id || !selected.position) return
    if (selected.position[0] === target[0] && selected.position[1] === target[1]) { removeMovementGoal(selected.id); if (selected.kind === 'CORE') setCoreAction(null); else setUnitAction(selected.id, null); select(null); return }
    const path = findMovementPath(game.state, game.explored, selected, target, planRef.current)
    if (!path.path || path.path.length < 2) { setMovementError(path.reason ?? 'NO_ROUTE'); return }
    const nextGoals = { ...movementGoalsRef.current, [selected.id]: target }
    const result = applyAutonomousMovement(game.state, game.explored, nextGoals, planRef.current)
    replaceMovementGoals(nextGoals); setMovementError(null); autoMovementTickRef.current = game.tick
    if (result.changed) commitManualPlan(result.plan)
    select(null)
  }
  const cancelMovementGoal = (object: WorldObject) => { if (!object.id) return; removeMovementGoal(object.id); if (object.kind === 'CORE') setCoreAction(null); else setUnitAction(object.id, null); select(null) }
  // Show the raw upstream error code alongside the friendly text: it is the
  // decisive clue when an order is rejected (CSRF_INVALID, TICK_MISMATCH...).
  const describeError = (code: string) => `${getErrorMessage(code)} [${code}]`
  const selectedWaypointName = selected?.kind === 'UNIT' ? unitDashboardName(selected, unitNames) : undefined
  const selectedWaypointEntry = selectedWaypointName ? waypoints[selectedWaypointName] : undefined
  if (!game.state) return <div className="grid h-dvh place-items-center"><div className="text-center"><div className="mx-auto mb-4 size-2 animate-pulse rounded-full bg-cyan-signal shadow-[0_0_14px_rgba(69,145,197,.45)]" /><p className="font-mono text-xs tracking-[.2em] text-zinc-500">{t(`game.${game.phase}`)}</p>{game.error && <p role="alert" className="mt-3 text-xs text-coral-hostile">{describeError(game.error)}</p>}</div></div>
  return <div className="grid h-dvh min-h-[560px] grid-cols-1 overflow-hidden lg:grid-cols-[260px_1fr]">
    <AssetList state={game.state} objects={game.state.objects} selectedId={selectedId} onSelect={selectFromAssetList} unitNames={unitNames} teamRoster={teamRoster} onAssignTeam={demo ? undefined : assignTeam} teamConfig={teamConfig} onUpdateConfig={demo ? undefined : updateConfig} onPickCoords={demo ? undefined : startCoordPick} pickingCoordsField={coordPick?.xField ?? null} />
    <section className="relative min-h-0 overflow-hidden">
      {!respawning && <GameHUD phase={game.phase} stateReceivedAt={game.stateReceivedAt} />}
      {!respawning && <EnemySightings state={game.state} onJump={jumpToEnemy} sightings={memoryEnemies} onJumpTo={jumpToMemoryEnemy} />}
      {!respawning && game.tick && <PendingCommands tick={game.tick} state={game.state} receipts={game.receipts} unitNames={unitNames} />}
      <WorldCanvas state={game.state} explored={game.explored} selectedId={selectedId} targeting={targetMode !== null} destinationSelecting={moveSelecting} attackPositions={attackPositions} targetableIds={targetableIds} routeDestinations={routeDestinations} moveArrows={moveArrows} sweepMarkers={sweepMarkers} shotMarkers={shotMarkers} centerPosition={centerPosition} centerRequest={centerRequest} zoomRequest={zoomRequest} onSelect={select} onTarget={chooseTarget} onAttackPosition={chooseAttackPosition} onMoveDestination={chooseMoveDestination} onCenterBeacon={() => { setCenterPosition(game.state!.champion_beacon.position); setCenterRequest((value) => value + 1) }} onCenterCore={() => { setCenterPosition(null); setCenterRequest((value) => value + 1) }} beaconIndicatorVisible={beaconIndicatorVisible} coreIndicatorVisible={coreIndicatorVisible} onAnchorChange={setAnchor} coordPicking={coordPick !== null || waypointPick !== null} onCoordPick={(position) => waypointPick ? completeWaypointPick(position) : completeCoordPick(position)} highlightPositions={[...coordPickHighlight, ...waypointPickHighlight]} memoryEnemies={memoryEnemies} />
      {!respawning && <ResourceActivity events={game.state.events} />}
      {respawning && <RespawnOverlay destroyedBy={coreDestroyer} selfDestructed={coreSelfDestructed} />}
      {!respawning && selected?.controlled && anchor && actionAvailability && !targetMode && !moveSelecting && !waypointPick && <UnitActionDialog anchor={anchor} selected={selected} plan={plan} movementGoal={selected.id ? movementGoals[selected.id] : undefined} unitNames={unitNames} phase={game.phase} resources={game.state.resources} population={game.state.population} availability={actionAvailability} waypointName={selectedWaypointName} waypointQueue={selectedWaypointEntry?.queue} waypointMode={selectedWaypointEntry?.mode} onPickWaypoint={demo ? undefined : () => startWaypointPick(selected)} onRemoveWaypoint={demo || !selectedWaypointName ? undefined : (index) => changeWaypoints(selectedWaypointName, index)} onClearWaypoints={demo || !selectedWaypointName ? undefined : () => changeWaypoints(selectedWaypointName)} onClose={() => select(null)} onTargeting={() => { setMoveSelecting(false); setTargetMode('SHOOT') }} onSweepTargeting={() => { setMoveSelecting(false); setTargetMode('SWEEP') }} onMoveTargeting={() => { setTargetMode(null); setMovementError(null); setMoveSelecting(true) }} onCancelMovementGoal={() => cancelMovementGoal(selected)} onUnitAction={unitAction} onCoreAction={coreAction} />}
      {targetMode && <div className="panel absolute left-1/2 top-28 z-30 flex -translate-x-1/2 items-center gap-2 rounded-full pl-4 pr-1.5 text-xs text-coral-hostile">{targetMode === 'SWEEP' ? <Sword size={15} /> : <Crosshair size={15} />}<span>{t(targetMode === 'SWEEP' ? 'game.sweepHint' : 'game.targetHint')}</span><button onClick={() => setTargetMode(null)} className="focus-ring ml-1 min-h-11 rounded-full px-3 text-zinc-400 hover:bg-white/5 hover:text-white">{t('common.cancel')}</button></div>}
      {moveSelecting && <div className={`panel absolute left-1/2 top-28 z-30 flex -translate-x-1/2 items-center gap-2 rounded-full pl-4 pr-1.5 text-xs ${movementError ? 'text-coral-hostile' : 'text-cyan-signal'}`}><Move size={15} /><span>{t(movementError === 'UNKNOWN_DESTINATION' ? 'game.routeUnknown' : movementError ? 'game.routeBlocked' : 'game.moveHint')}</span><button onClick={() => { setMoveSelecting(false); setMovementError(null) }} className="focus-ring ml-1 min-h-11 rounded-full px-3 text-zinc-400 hover:bg-white/5 hover:text-white">{t('common.cancel')}</button></div>}
      {coordPick && <div className="panel absolute left-1/2 top-28 z-30 flex -translate-x-1/2 items-center gap-2 rounded-full pl-4 pr-1.5 text-xs text-cyan-signal"><Crosshair size={15} /><span>{t('game.coordPickHint')}</span><button onClick={() => setCoordPick(null)} className="focus-ring ml-1 min-h-11 rounded-full px-3 text-zinc-400 hover:bg-white/5 hover:text-white">{t('common.cancel')}</button></div>}
      {waypointPick && <div className="panel absolute left-1/2 top-28 z-30 flex -translate-x-1/2 items-center gap-2 rounded-full pl-4 pr-1.5 text-xs text-cyan-signal"><Crosshair size={15} /><span>{t('game.waypointPickHint', { name: waypointPick.name })}</span><button onClick={() => setWaypointPick(null)} className="focus-ring ml-1 min-h-11 rounded-full px-3 text-zinc-400 hover:bg-white/5 hover:text-white">{t('common.cancel')}</button></div>}
      {!respawning && <MapControls onCenter={() => { setCenterPosition(null); setCenterRequest((value) => value + 1) }} onZoom={(direction) => setZoomRequest((value) => direction * (Math.abs(value) + 1))} beaconIndicatorVisible={beaconIndicatorVisible} onToggleBeaconIndicator={toggleBeaconIndicator} coreIndicatorVisible={coreIndicatorVisible} onToggleCoreIndicator={toggleCoreIndicator} memoryVisible={enemyMemoryVisible} onToggleMemory={toggleEnemyMemory} memoryFilters={new Set(enemyMemoryFilters)} onToggleMemoryFilter={toggleEnemyMemoryFilter} />}
      {game.error && <div role="alert" className="panel absolute bottom-4 right-4 z-30 max-w-[min(24rem,calc(100%-2rem))] px-4 py-3 text-xs leading-5 text-coral-hostile">{describeError(game.error)}</div>}
    </section>
  </div>
}
