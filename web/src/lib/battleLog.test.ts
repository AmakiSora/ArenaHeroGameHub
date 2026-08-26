import { afterEach, describe, expect, it, vi } from 'vitest'
import { battleLogLimitFor, loadBattleLog, splitLogMessage } from './battleLog'

const respond = (payload: unknown, ok = true) => vi.fn(async () => ({
  ok,
  json: async () => payload,
}) as Response)

describe('loadBattleLog', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('normalizes entries newest-first as served', async () => {
    vi.stubGlobal('fetch', respond({
      ok: true,
      entries: [
        { tick: 9, ts: 1700000000, cat: 'kill', msg: '击杀 (1,2)' },
        { tick: null, ts: 1700000001.5, cat: 'config', msg: '配置调整' },
        { tick: 'bad', ts: null, cat: 'unknown-cat', msg: '一行' },
      ],
    }))

    await expect(loadBattleLog()).resolves.toEqual([
      { tick: 9, ts: 1700000000, cat: 'kill', msg: '击杀 (1,2)' },
      { tick: null, ts: 1700000001.5, cat: 'config', msg: '配置调整' },
      { tick: null, ts: null, cat: '', msg: '一行' },
    ])
  })

  it('drops entries without a message and tolerates request failures', async () => {
    vi.stubGlobal('fetch', respond({
      ok: true,
      entries: [{ tick: 1, cat: 'warn' }, null, { msg: '' }, { tick: 2, cat: 'warn', msg: '保留' }],
    }))
    await expect(loadBattleLog()).resolves.toEqual([
      { tick: 2, ts: null, cat: 'warn', msg: '保留' },
    ])

    vi.stubGlobal('fetch', respond({}, false))
    await expect(loadBattleLog()).resolves.toEqual([])

    vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('network') }))
    await expect(loadBattleLog()).resolves.toEqual([])
  })

  it('asks the server for the requested row limit', async () => {
    const fetchMock = respond({ ok: true, entries: [] })
    vi.stubGlobal('fetch', fetchMock)
    await loadBattleLog(600)
    expect(fetchMock).toHaveBeenCalledWith('/api/battle-log?limit=600', expect.anything())
  })
})

describe('splitLogMessage', () => {
  it('splits (x,y) coordinates into clickable segments', () => {
    expect(splitLogMessage('W1 击中 E3 造成 3 伤害 (5,5)→(6,5)')).toEqual([
      { kind: 'text', text: 'W1 击中 E3 造成 3 伤害 ' },
      { kind: 'coord', text: '(5,5)', position: [5, 5] },
      { kind: 'text', text: '→' },
      { kind: 'coord', text: '(6,5)', position: [6, 5] },
    ])
  })

  it('handles negative coordinates and inner spaces', () => {
    expect(splitLogMessage('发现 (-3, 4)')).toEqual([
      { kind: 'text', text: '发现 ' },
      { kind: 'coord', text: '(-3, 4)', position: [-3, 4] },
    ])
  })

  it('returns the whole message when it carries no coordinates', () => {
    expect(splitLogMessage('容器重启')).toEqual([{ kind: 'text', text: '容器重启' }])
  })
})

describe('battleLogLimitFor', () => {
  it('scales the fetch limit with the selected time window', () => {
    expect(battleLogLimitFor('all')).toBe(3000)
    expect(battleLogLimitFor(21600)).toBe(2000)
    expect(battleLogLimitFor(3600)).toBe(1000)
    expect(battleLogLimitFor(1800)).toBe(600)
    expect(battleLogLimitFor(600)).toBe(300)
    expect(battleLogLimitFor(42)).toBe(300)
  })
})
