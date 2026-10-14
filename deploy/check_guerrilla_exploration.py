#!/usr/bin/env python3
"""Check whether guerrilla exploration is actually improving on production.

Read-only over SSH: it reads /app/runtime inside the container and writes
nothing anywhere. Run it on a schedule; it prints a verdict and exits non-zero
when something looks wrong, so a failure shows up as a failed run rather than as
silence.

Everything is measured since the last container restart marker in
tactic_play.log, i.e. since the deployment that shipped the current policy — no
tick numbers to keep updated by hand.

Baselines this compares against (all measured, not invented):
  fleet rewalk 9.20%  = the 15h window before guerrilla_frontier_roam
  rewalk 19.3%        = the 3-day baseline before any of these gates
  max gap 500         = the orbit-break threshold in tactic.py
  >8 breaks/unit-day  = the flapping suspicion bar
"""
from __future__ import annotations

import argparse
import json
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import time
from collections import Counter, defaultdict
from pathlib import Path

import paramiko

ENV_FILE = Path(__file__).resolve().parent / ".env.deploy"

REWALK_REGRESS_PCT = 19.3      # pre-gate fleet baseline
REWALK_WATCH_PCT = 9.2         # pre-frontier fleet baseline
COLLAPSE_DISTINCT = 600        # distinct cells under this with 800+ steps = collapsed
COLLAPSE_MIN_STEPS = 800
FLAP_PER_UNIT_DAY = 8.0
SLOW_SHARE_WATCH = 45.0        # % of ticks over the 3000ms logging threshold
MAPWRITE_5S_WATCH = 200        # ticks whose map_memory phase cost >=5s
FREEZE_STREAK_WATCH = 40       # consecutive WAIT ticks for one roaming unit


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


def connect() -> tuple[paramiko.SSHClient, str]:
    env = load_env()
    host = env.get("DEPLOY_HOST") or sys.exit("DEPLOY_HOST not set")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        host, int(env.get("DEPLOY_PORT", "22")),
        env.get("DEPLOY_USER", "root"), env.get("DEPLOY_PASSWORD", ""), timeout=25,
    )
    return client, env.get("DEPLOY_REMOTE_BASE", "/srv/arena-game")


def run(client: paramiko.SSHClient, cmd: str, timeout: int = 600) -> str:
    _stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace").strip()
    if err:
        print(f"  [stderr] {err[:400]}")
    return out


def stats(steps: list[tuple[int, int]]) -> dict:
    """Per-unit trail statistics from a chronological list of positions."""
    seen: Counter = Counter()
    gap = max_gap = rewalk = 0
    xs: list[int] = []
    ys: list[int] = []
    for cell in steps:
        prior = seen[cell]
        if prior == 0:
            max_gap = max(max_gap, gap)
            gap = 0
        else:
            gap += 1
        if prior >= 3:
            rewalk += 1
        seen[cell] += 1
        xs.append(cell[0])
        ys.append(cell[1])
    max_gap = max(max_gap, gap)
    total = len(steps)
    return {
        "steps": total,
        "distinct": len(seen),
        "rewalk": 100.0 * rewalk / total if total else 0.0,
        "max_gap": max_gap,
        "bbox": (max(xs) - min(xs) + 1, max(ys) - min(ys) + 1) if total else (0, 0),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--container", default="arena-game-app-1")
    args = parser.parse_args()
    client, base = connect()
    ex = f"cd {base} && docker compose exec -T app"
    play = "/app/runtime/tactic_play.log"
    alarms: list[str] = []
    notes: list[str] = []

    print(f"== 游击探索效果检查 @ {time.strftime('%Y-%m-%d %H:%M:%S')} ==")

    health = run(
        client,
        "docker inspect --format '{{.State.Status}} {{.State.Health.Status}} "
        f"restarts={{{{.RestartCount}}}} started={{{{.State.StartedAt}}}}' {args.container}",
    ).strip()
    print(f"容器: {health}")
    if "unhealthy" in health:
        alarms.append("容器 unhealthy")

    # The window: everything after the last lifecycle marker = this deployment.
    marker_out = run(
        client, f"{ex} sh -c 'grep -n \"\\[entrypoint\\]\" {play} | tail -1'"
    ).strip()
    if not marker_out:
        print("找不到 entrypoint 标记，无法定位部署边界")
        client.close()
        return 1
    cut = int(marker_out.split(":", 1)[0])
    tail = run(client, f"{ex} sh -c 'tail -n +{cut + 1} {play}'").splitlines()
    tick_lines = [l for l in tail if l.startswith("tick=") and "enemies=" in l]
    ticks = [int(l.split("tick=")[1].split()[0]) for l in tick_lines]
    if not ticks:
        print("部署后还没有出拍记录")
        client.close()
        return 1
    start, end = ticks[0], ticks[-1]
    span = end - start + 1
    print(f"窗口: tick {start}..{end}（{span} 拍，出拍行 {len(tick_lines)}）")

    latency = sorted(
        int(l.split("latency=")[1].split("ms")[0]) for l in tail if "latency=" in l
    )
    share = 100.0 * len(latency) / len(tick_lines)
    mapwrite = sum(
        1 for l in tail
        if "map_memory=" in l and "latency=" in l
        and int(l.split("map_memory=")[1].split("ms")[0]) >= 5000
    )
    if latency:
        mid = latency[len(latency) // 2]
        high = latency[int(len(latency) * 0.95) - 1]
        print(
            f"延迟: 慢拍占比 {share:.1f}% mean={sum(latency) // len(latency)} "
            f"p50={mid} p95={high} max={latency[-1]}｜map_memory>=5s 共 {mapwrite} 拍"
        )
    else:
        print(f"延迟: 窗口内没有超过记录阈值的拍")
    if share > SLOW_SHARE_WATCH:
        notes.append(f"慢拍占比 {share:.1f}%（上线前 26.9%->71.0%）——瓶颈是 tactic 自己的 map_memory 写盘，与本次改动无关，但值得单独修")
    if mapwrite > MAPWRITE_5S_WATCH:
        notes.append(f"map_memory 相位 >=5s 共 {mapwrite} 拍，写盘节流仍是第一位待办")
    walls = [int(l.split("walls=")[1]) for l in tick_lines if "walls=" in l]
    if walls:
        print(f"墙记忆: {walls[-1]:,} 格（窗口起点 {walls[0]:,}，+{walls[-1] - walls[0]:,}）")
    enemies = Counter(l.split("enemies=")[1].split()[0] for l in tick_lines)
    engaged = sum(v for k, v in enemies.items() if k != "0" and int(k) < 100)
    print(f"可见敌人: {engaged}/{len(tick_lines)} 拍有敌人（{100.0 * engaged / len(tick_lines):.1f}%）")

    trace = sum(1 for l in tail if "Traceback" in l)
    mismatch = sum(1 for l in tail if "TICK_MISMATCH" in l)
    print(f"异常: Traceback={trace} TICK_MISMATCH={mismatch}（每千拍 {1000.0 * mismatch / span:.1f}）")
    if trace:
        kinds = Counter()
        for index, line in enumerate(tail):
            if "Traceback" in line:
                for follow in tail[index + 1:index + 22]:
                    stripped = follow.strip()
                    if stripped and not stripped.startswith(("File", "^", "Traceback")) \
                            and not follow.startswith("    "):
                        kinds[stripped[:60]] += 1
                        break
        print(f"  异常类型: {dict(kinds)}")
        if not all("409" in k for k in kinds):
            alarms.append(f"出现非 409 的新异常类型: {sorted(kinds)}")

    breaks = run(client, f"{ex} sh -c 'grep -c orbit-break {play}'").strip()
    fuse = run(client, f"{ex} sh -c 'grep -c sweep-fuse {play}'").strip()
    fired = [
        line.split("uid=")[1].split()[0]
        for line in tail if "[orbit-break]" in line and "uid=" in line
    ]
    window_breaks = len(fired)
    print(f"标记: orbit-break 全文件 {breaks} 次 / 本窗口 {window_breaks} 次；sweep-fuse 全文件 {fuse} 次")
    if fired:
        print(f"  本窗口触发 uid: {fired}")
    if fuse == "0" and engaged == 0:
        print("  sweep-fuse 为 0 且窗口内没有可见敌人：该闸本轮依旧没有可触发的事件，不能记为已验证")

    # Trail quality: the number that actually decides whether this worked.
    days = sorted(
        name.split("guerrilla_routes-")[1].split(".jsonl")[0]
        for name in run(
            client, f"{ex} sh -c 'ls /app/runtime/guerrilla_routes-*.jsonl 2>/dev/null'"
        ).split()
    )
    if not days:
        print("没有游击轨迹文件")
        client.close()
        return 1
    raw = ""
    for day in days[-2:]:
        raw += run(client, f"{ex} cat /app/runtime/guerrilla_routes-{day}.jsonl")
    by_unit: dict[str, list] = defaultdict(list)
    skipped = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1          # the last line is often a concurrent-append torn
            continue
        if not isinstance(row, dict) or not {"t", "p", "i", "u"} <= set(row):
            skipped += 1
            continue
        if row["t"] >= start:
            by_unit[row["i"]].append(row)

    steps = sum(len(v) for v in by_unit.values())
    epochs = [r["e"] for entries in by_unit.values() for r in entries if "e" in r]
    hours = max(0.1, (max(epochs) - min(epochs)) / 3600.0) if epochs else span / 223.0
    re_total = 0
    rows_out = []
    for uid, entries in by_unit.items():
        if len(entries) < 200:
            continue
        data = stats([(int(r["p"][0]), int(r["p"][1])) for r in sorted(entries, key=lambda r: r["t"])])
        re_total += round(data["rewalk"] * data["steps"] / 100.0)
        rows_out.append((uid, entries[-1]["u"], data))
    if rows_out:
        per_unit_day = window_breaks / len(rows_out) / hours * 24.0
        print(f"窗口时长 {hours:.1f}h；触发密度 {per_unit_day:.1f} 次/单位-日（抖动线 {FLAP_PER_UNIT_DAY}）")
        if per_unit_day > FLAP_PER_UNIT_DAY:
            alarms.append(f"orbit-break 触发密度 {per_unit_day:.1f}/单位-日 超线，可能在反复顶同一个口袋")
    rows_out.sort(key=lambda item: -item[2]["rewalk"])
    fleet = 100.0 * re_total / steps if steps else 0.0
    print(f"\n轨迹: {len(rows_out)} 匹游击单位 / {steps:,} 步（跳过截断行 {skipped}）")
    print(f"  {'名':<5}{'id':<11}{'步数':>7}{'distinct':>9}{'重走%':>8}{'最长无新格':>11}  bbox")
    for uid, name, data in rows_out:
        flag = ""
        if data["steps"] >= COLLAPSE_MIN_STEPS and data["distinct"] < COLLAPSE_DISTINCT:
            flag = "  <-- 塌缩"
            alarms.append(f"{name}({uid}) 塌缩：{data['steps']} 步只有 {data['distinct']} 格")
        if data["max_gap"] >= 500 and not any(full.startswith(uid) for full in fired):
            flag += "  <-- 越过 500 判据却没有触发记录"
            alarms.append(f"{name}({uid}) 最长无新格 {data['max_gap']} 拍 >=500，但窗口内没有它的触发记录")
        print(f"  {name:<5}{uid:<11}{data['steps']:>7}{data['distinct']:>9}"
              f"{data['rewalk']:>7.1f}%{data['max_gap']:>11}  "
              f"{data['bbox'][0]}x{data['bbox'][1]}{flag}")
    print(f"\n全网重走率 {fleet:.2f}%  （前沿探索上线前 9.20%，各道闸上线前 {REWALK_REGRESS_PCT}%）")
    if fleet > REWALK_WATCH_PCT:
        alarms.append(f"重走率 {fleet:.2f}% 未优于上线前的 9.20%")

    client.close()
    if notes:
        print("\n观察项（不影响退出码）:")
        for note in notes:
            print(f"  - {note}")
    print("\n" + ("[ALARM] 告警:\n  - " + "\n  - ".join(alarms) if alarms else "[OK] 无告警"))
    return 1 if alarms else 0


if __name__ == "__main__":
    sys.exit(main())
