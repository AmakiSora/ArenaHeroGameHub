"""Real-time tactic decision status viewer.
Run: python status.py
Shows latest tick state, worker assignments, and resource memory.
"""
import json
import os
import time
from collections import defaultdict

LOG_FILE = "tactic_log.jsonl"

def read_latest():
    """Read the latest complete tick record from the log."""
    if not os.path.exists(LOG_FILE):
        print("[ERROR] tactic_log.jsonl not found")
        return None
    
    with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
        lines = [l.strip() for l in f if l.strip()]
    
    if not lines:
        print("[INFO] Log is empty")
        return None
    
    # Find the latest tick record
    latest = None
    for line in reversed(lines):
        try:
            rec = json.loads(line)
            if rec.get("tick") and "plan_unit_actions" in rec:
                latest = rec
                break
        except:
            continue
    
    return latest


def read_history(ticks=20):
    """Read the last N tick records."""
    if not os.path.exists(LOG_FILE):
        return []
    
    with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
        lines = [l.strip() for l in f if l.strip()]
    
    history = []
    for line in reversed(lines):
        try:
            rec = json.loads(line)
            if rec.get("tick") and "plan_unit_actions" in rec:
                history.append(rec)
                if len(history) >= ticks:
                    break
        except:
            continue
    
    return history


def format_pos(pos):
    if not pos:
        return "?"
    return f"({pos[0]:3d},{pos[1]:3d})"


def show_assignments(rec):
    """Try to infer resource assignments from the tick."""
    resources = rec.get("resource_cells", [])
    workers = rec.get("workers", [])
    actions = rec.get("plan_unit_actions", {})
    
    # Show which workers are going to resources
    assignment_info = defaultdict(list)
    for w in workers:
        wid = w.get("id", "")
        action = actions.get(wid, "")
        cargo = w.get("cargo", 0)
        pos = w.get("pos", [])
        
        if "->" in action and cargo == 0:
            # Worker is moving toward a goal
            goal_str = action.split("->")[-1].strip()
            assignment_info["to_resource"].append((wid, tuple(pos), goal_str))
        elif cargo > 0:
            assignment_info["to_core"].append((wid, tuple(pos), cargo))
        elif "explore" in action:
            assignment_info["exploring"].append((wid, tuple(pos)))
        elif "HARVEST" in action:
            assignment_info["harvesting"].append((wid, tuple(pos)))
        elif "DEPOSIT" in action:
            assignment_info["depositing"].append((wid, tuple(pos)))
        elif "WAIT" in action:
            assignment_info["WAITING"].append((wid, tuple(pos)))
    
    return assignment_info


def show_status():
    rec = read_latest()
    if not rec:
        return
    
    print("=" * 70)
    print(f"  TICK {rec['tick']}  |  Log age: {time.time() - os.path.getmtime(LOG_FILE):.0f}s")
    print("=" * 70)
    
    # Core
    core_pos = rec.get("core_pos")
    core_action = rec.get("core_action", "?")
    print(f"  Core: {format_pos(core_pos)}  →  {core_action}")
    
    # Resources
    r_visible = rec.get("resource_cells_visible", 0)
    r_cells = rec.get("resource_cells", [])
    r_memory = rec.get("memory", 0)
    print(f"  Resources: {r_visible} visible {r_cells}  |  memory={r_memory}")
    
    # Enemies
    enemies = rec.get("enemies", 0)
    print(f"  Enemies: {enemies}")
    
    # Workers
    workers = rec.get("workers", [])
    actions = rec.get("plan_unit_actions", {})
    print(f"\n  Workers ({len(workers)}):")
    print(f"  {'ID':<10} {'POS':<15} {'CARGO':<6} {'ACTION':<30}")
    print(f"  {'-'*10} {'-'*15} {'-'*6} {'-'*30}")
    for w in workers:
        wid = w.get("id", "")[:8]
        pos = format_pos(w.get("pos", []))
        cargo = w.get("cargo", 0)
        action = actions.get(w.get("id", ""), "?")
        marker = "[H]" if "HARVEST" in action else "[C]" if cargo else "[S]" if "explore" in action else "[W]" if "WAIT" in action else "[D]" if "DEPOSIT" in action else "[?]"
        print(f"  {marker} {wid:<8} {pos:<15} {cargo:<6} {action:<30}")
    
    # Vanguards
    vgs = rec.get("vanguards", [])
    if vgs:
        print(f"\n  Vanguards ({len(vgs)}):")
        for v in vgs:
            vid = v.get("id", "")[:8]
            pos = format_pos(v.get("pos", []))
            hp = v.get("hp", "?")
            action = actions.get(v.get("id", ""), "?")
            print(f"  [VG] {vid:<8} {pos:<15} HP={hp:<3} {action:<30}")
    
    # Rangers
    rgs = rec.get("rangers", [])
    if rgs:
        print(f"\n  Rangers ({len(rgs)}):")
        for r in rgs:
            rid = r.get("id", "")[:8]
            pos = format_pos(r.get("pos", []))
            hp = r.get("hp", "?")
            action = actions.get(r.get("id", ""), "?")
            print(f"  [RG] {rid:<8} {pos:<15} HP={hp:<3} {action:<30}")
    
    # Summary
    print(f"\n  --- Summary ---")
    inf = show_assignments(rec)
    for k, v in inf.items():
        print(f"  {k}: {len(v)}")
    
    # Check for stuck workers
    print()
    check_stuck()


def check_stuck():
    """Check if any worker has been at the same position for many ticks."""
    history = read_history(25)
    if len(history) < 5:
        return
    
    workers_pos = defaultdict(list)
    for rec in history:
        for w in rec.get("workers", []):
            wid = w.get("id", "")
            pos = tuple(w.get("pos", []))
            workers_pos[wid].append(pos)
    
    for wid, positions in workers_pos.items():
        if len(positions) < 5:
            continue
        recent = positions[:8]
        if len(set(recent)) == 1:
            print(f"  [STUCK] {wid[:8]} at {recent[0]} for {len(positions)} ticks!")
        elif len(set(recent)) <= 2 and len(set(recent)) < len(recent):
            # Oscillating between 2 positions
            unique = list(set(recent))
            print(f"  [OSCILLATE] {wid[:8]} between {unique}")
        else:
            moving = sum(1 for i in range(1, len(recent)) if recent[i] != recent[i-1])
            if moving < len(recent) * 0.3:
                print(f"  [SLOW] {wid[:8]} moving {moving}/{len(recent)} ticks")


if __name__ == "__main__":
    show_status()