import { useCallback, useEffect, useRef, useState } from 'react'
import { api, APIError, apiURL } from '../lib/api'
import { demoReceipt, demoState } from '../lib/demo'
import { isReceivedNotice, type CommandReceipts } from '../lib/commandPlans'
import { loadExplored, rememberVisible, type ExploredCell } from '../lib/exploration'
import type { CommandPlan, PlayerState, ReceivedNotice, StreamPhase } from '../lib/types'

type GameMessage =
  | { type: 'tick'; data: number }
  | { type: 'state'; data: PlayerState }
  | { type: 'received'; data: ReceivedNotice }

const reconnectBaseMs = 250
const reconnectMaxMs = 5_000
// Ticks arrive roughly every 15s; a silent socket for three ticks is a
// half-dead connection (NAT drop, stalled proxy) and must be recycled.
const staleStreamMs = 45_000
const watchdogIntervalMs = 5_000

function gameWebSocketURL() {
  const url = new URL(apiURL('/api/v1/game/ws'), window.location.href)
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  return url.toString()
}

function reconnectDelay(attempt: number) {
  const exponential = reconnectBaseMs * (2 ** Math.min(attempt, 8))
  const bounded = Math.min(reconnectMaxMs, exponential)
  return Math.round(bounded * (0.8 + Math.random() * 0.4))
}

export function useGameStream(demo = false, explorationNamespace = 'anonymous') {
  const [tick, setTick] = useState<number | null>(demo ? 10583 : null)
  const [state, setState] = useState<PlayerState | null>(demo ? demoState : null)
  const [phase, setPhase] = useState<StreamPhase>(demo ? 'open' : 'connecting')
  const [stateReceivedAt, setStateReceivedAt] = useState<number | null>(() => demo ? Date.now() : null)
  const [receipts, setReceipts] = useState<CommandReceipts>({})
  const [explored, setExplored] = useState<Map<string, ExploredCell>>(new Map())
  const [error, setError] = useState('')
  const tickRef = useRef<number | null>(tick)
  const exploredRef = useRef(explored)
  // Merge without copying when nothing changed: the explored map can hold
  // tens of thousands of cells, and a fresh identity every tick would
  // invalidate the terrain tile cache and re-run pathfinding for nothing.
  const mergeExplored = useCallback((cells: Map<string, ExploredCell>) => {
    const current = exploredRef.current
    let next = current
    for (const [key, cell] of cells) {
      const previous = current.get(key)
      if (previous && previous.kind === cell.kind) continue
      if (next === current) next = new Map(current)
      next.set(key, cell)
    }
    if (next === current) return
    exploredRef.current = next
    setExplored(next)
  }, [])

  useEffect(() => {
    const empty = new Map<string, ExploredCell>()
    exploredRef.current = empty
    setExplored(empty)
    void loadExplored(explorationNamespace).then(mergeExplored).catch(() => undefined)
  }, [explorationNamespace, mergeExplored])
  useEffect(() => {
    if (demo) { void rememberVisible(explorationNamespace, demoState, exploredRef.current).then(mergeExplored).catch(() => undefined); return }
    let socket: WebSocket | null = null
    let reconnectTimer: number | null = null
    let reconnectAttempt = 0
    let stopped = false
    let lastFrameAt = Date.now()

    const scheduleReconnect = () => {
      if (stopped || reconnectTimer !== null) return
      const delay = reconnectDelay(reconnectAttempt)
      reconnectAttempt += 1
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = null
        connect()
      }, delay)
    }

    const connect = () => {
      if (stopped) return
      setPhase((current) => current === 'offline' ? 'connecting' : current)
      const next = new WebSocket(gameWebSocketURL())
      socket = next
      next.onopen = () => {
        if (socket !== next) return
        reconnectAttempt = 0
        lastFrameAt = Date.now()
        setError('')
        setPhase((current) => current === 'offline' ? 'connecting' : current)
      }
      next.onmessage = (event) => {
        if (socket !== next) return
        lastFrameAt = Date.now()
        try {
          const message = JSON.parse(String(event.data)) as GameMessage
          if (message.type === 'tick') {
            if (!Number.isSafeInteger(message.data) || message.data < 0) throw new Error('invalid tick')
            tickRef.current = message.data; setTick(message.data); setPhase('syncing'); setReceipts({}); setError('')
            return
          }
          if (message.type === 'state') {
            if (!message.data || typeof message.data !== 'object') throw new Error('invalid state')
            setState(message.data); setStateReceivedAt(Date.now()); setPhase('open'); void rememberVisible(explorationNamespace, message.data, exploredRef.current).then(mergeExplored).catch(() => undefined)
            return
          }
          if (message.type === 'received') {
            if (!isReceivedNotice(message.data) || message.data.tick !== tickRef.current) throw new Error('invalid receipt')
            setReceipts((current) => ({ ...current, [message.data.source]: message.data }))
            return
          }
          throw new Error('unknown message')
        } catch {
          setError('STATE_INVALID')
          next.close(1002, 'invalid server message')
        }
      }
      next.onerror = () => {
        if (socket === next) setPhase('offline')
      }
      next.onclose = (event) => {
        if (socket !== next) return
        socket = null
        setPhase('offline')
        if (stopped) return
        if (event.code === 1008) {
          setError('UNAUTHORIZED')
          return
        }
        // A server-side normal close (dashboard restart, upstream maintenance)
        // still deserves a reconnect; only unmount stops the loop for good.
        scheduleReconnect()
      }
    }

    // Recycle half-dead sockets: the proxy pipe can stay "open" long after
    // the upstream stopped delivering frames, leaving the countdown at 0.
    const watchdog = window.setInterval(() => {
      if (stopped || !socket || socket.readyState !== WebSocket.OPEN) return
      if (Date.now() - lastFrameAt > staleStreamMs) socket.close(4001, 'stale stream')
    }, watchdogIntervalMs)
    const onVisibility = () => {
      if (stopped || document.visibilityState !== 'visible') return
      if (!socket || socket.readyState === WebSocket.CLOSED || socket.readyState === WebSocket.CLOSING) {
        if (reconnectTimer !== null) { window.clearTimeout(reconnectTimer); reconnectTimer = null }
        connect()
      } else if (socket.readyState === WebSocket.OPEN && Date.now() - lastFrameAt > staleStreamMs) {
        socket.close(4001, 'stale stream')
      }
    }
    document.addEventListener('visibilitychange', onVisibility)

    connect()
    return () => {
      stopped = true
      window.clearInterval(watchdog)
      document.removeEventListener('visibilitychange', onVisibility)
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer)
      if (socket) socket.close(1000, 'component unmounted')
    }
  }, [demo, explorationNamespace, mergeExplored])

  const submit = useCallback(async (plan: CommandPlan) => {
    setError('')
    try {
      const receipt = demo ? { ...demoReceipt, tick: plan.tick, received_at: new Date().toISOString() } : await api.submitCommands(plan)
      if (demo) {
        setReceipts((current) => ({
          ...current,
          MANUAL: { tick: receipt.tick, source: 'MANUAL', received_at: receipt.received_at, plan },
        }))
      }
      return receipt
    } catch (cause) {
      const code = cause instanceof APIError ? cause.code : 'REQUEST_FAILED'
      // Always surface the code: swallowing it when the Tick moved on left
      // operators staring at a UI that never explains a failed click.
      setError(code)
      if (code === 'COMMAND_WINDOW_CLOSED' || code === 'TICK_MISMATCH') setPhase('settling')
      throw cause
    }
  }, [demo])

  return { tick, state, phase, stateReceivedAt, receipts, explored, error, submit }
}