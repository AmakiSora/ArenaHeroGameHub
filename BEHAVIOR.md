# Arena Hero 战术脚本行为逻辑

## 概述

`tactic.py` 是一个 WebSocket 事件驱动的自动战术脚本，通过 `arena-hero` SDK 连接游戏服务器，每 Tick 接收一次完整状态，决策后提交计划。

---

## 一、总体架构

```
server --WebSocket state--> SDK --Turn--> choose_actions() --plan--> SDK --HTTP POST--> server
```

- 非轮询，WebSocket 长连接，事件驱动
- 每 Tick 收到 `Turn` 后立即决策提交
- 所有决策基于当前 Tick 的 `state`，不跨 Tick 复用控制器

---

## 二、资源记忆系统

### `_resource_memory: set[tuple[int, int]]`

全局变量，跨 Tick 持久化，记录所有已知矿点坐标。

**写入规则：**
- 任何 `turn.resource_cells` 中的矿点 → 自动加入记忆

**删除规则：**
- 本队工人成功采集（`HARVEST_SUCCEEDED`）→ 删除
- 采集失败提示矿已耗尽（`HARVEST_FAILED / RESOURCE_DEPLETED`）→ 删除
- 有友方单位（Core / Worker / Vanguard / Ranger）在 5 格内看到该格但无矿 → 删除（别人采走了）

**使用规则：**
- 工人无货且视野内无矿时 → 导航到最近记忆矿点

---

## 三、单位行为

### 3.1 Worker（工人）

5 个 Worker（手动生成），优先级从高到低：

| 优先级 | 条件 | 动作 | 说明 |
|--------|------|------|------|
| 1 | 有货 + 在 Core 位置 + 仓库有空间 | **DEPOSIT** | 卸货，资源入库 |
| 2 | 有货 + 不在 Core 位置 | **MOVE** → Core | 回基地，方向被挡时尝试其他 3 方向 |
| 3 | 无货 + 站在矿点上 | **HARVEST** | 采集，1 次采 1 单位 |
| 4 | 无货 + 视野内有矿 | **MOVE** → 最近矿点 | 优先选距离最近的矿 |
| 5 | 无货 + 无可见矿 + 记忆中有矿 | **MOVE** → 最近记忆矿点 | 导航到已知矿点 |
| 6 | 无货 + 无任何矿 | **MOVE** UUID 方向探索 | 按 UUID 哈希选方向（上/右/下/左），每个工人不同 |

**方向受阻处理：**
- 首选方向被障碍挡住时，按**离目标距离排序**尝试其他 3 方向
- 全部被挡 → 执行 UUID 探索

### 3.2 Vanguard（先锋）

1 个 Vanguard（手动生成），优先级：

| 优先级 | 条件 | 动作 | 说明 |
|--------|------|------|------|
| 1 | 相邻格有敌人 | **SWEEP** | 对相邻格扇形攻击，1 伤害 |
| 2 | 有可见敌人但不相邻 | **MOVE** → 最近敌人 | 靠近敌人 |
| 3 | 无敌人 | **MOVE** UUID 方向巡游 | 优先向上探索 |

**设计目的：** 先锋探路，发现敌人后主动接战，掩护工人。

### 3.3 Ranger（游侠）

1 个 Ranger（手动生成），优先级：

| 优先级 | 条件 | 动作 | 说明 |
|--------|------|------|------|
| 1 | 敌人在射程 1-3 格 + 无障碍 | **SHOOT** | 远程攻击，1 伤害 |
| 2 | 有可见敌人但超出射程 | **MOVE** → 最近敌人 | 进入射程 |
| 3 | 无敌人 | **MOVE** UUID 方向巡游 | 按 UUID 哈希选方向 |

**设计目的：** 游侠远程支援，视野 5 格比 Vanguard 还远，没敌人时也巡游。

---

## 四、Core（总部）行为

Core 不自动生产单位，完全手动控制。

| 优先级 | 条件 | 动作 | 说明 |
|--------|------|------|------|
| 1 | 脚下有 Beacon（地面） | **PICKUP_BEACON** | 捡取冠军信标 |
| 2 | 盾不满 + 资源 ≥ 1 | **REPAIR_SHIELD** | 1 资源修 1 盾 |
| 3 | 有带矿工人在 5 格内 | **WAIT** | 停等工人卸货 |
| 4 | 无带矿工人 | **START_MOVE** → 目标方向 | 朝工人中心或最近矿点移动 |

**移动方向计算：**
- 计算所有工人平均位置
- 计算最近矿点（可见 + 记忆）
- 选两者中离 Core 更近的作为目标
- 优先尝试主方向，被挡则尝试其他方向

**移动节奏：** Core 每 4 Tick 走 1 格，迁移期间不能接收卸货。

---

## 五、日志系统

### 控制台输出（每 Tick）
```
tick=35060 core=MOVE_RIGHT res=9/45 pop=9 workers=7 enemies=0 resources_visible=1 memory=1
```

### JSONL 日志（`tactic_log.jsonl`）
每 Tick 记录完整状态到 JSONL 文件，包含：
- Tick 编号、时间戳
- Core 位置、HP、盾、状态、动作
- 资源数、容量、人口、阶层、维护费
- 所有 Worker / Vanguard / Ranger 的位置、货物、HP
- 可见敌人数量、可见矿点、记忆矿点数
- 每单位发出的指令
- 上 Tick 的解析事件（采集成功/失败、卸货成功等）
- 决策耗时

### 运行结束汇总
```
TACTIC SUMMARY
  Ticks played:     347
  Harvest actions:  42
  Harvest success:  38
  Deposit success:  35
  Move actions:     285
```

---

## 六、Git 提交历史

```
f294d38 fix: auto-clean harvested resources from memory when units see empty cell
e28921b feat: Ranger scouts when no enemies, moves toward visible enemies out of range
10f3f7c fix: sort fallback directions by distance to goal, stop oscillation
b275780 fix: Core only stops when cargo worker within 5 cells, not globally
d489dbb fix: remove duplicate logging block that caused UnboundLocalError crash
f1e690b feat: Core migrates toward resource/worker center, stops for cargo workers
d557417 fix: workers try all directions when blocked instead of WAIT
ffa5cc1 feat: resource memory, worker fan-out by UUID, Vanguard scout
b558d97 feat: add direct-play wrapper with BFS pathfinding, enemy avoidance, auto-restart
02cae7e chore: add .gitignore for logs and generated files
97c2011 init: Arena Hero balanced tactic script
```

---

## 七、文件结构

```
tactic.py          # 主战术脚本（核心逻辑）
direct_wrapper.py  # 直接控制桥接器（备用，BFS 寻路）
requirements.txt   # 依赖：arena-hero>=0.2.4,<0.3
tactic_log.jsonl   # 运行日志（JSONL 格式，可分析）
tactic_play.log    # 控制台输出记录
```

## 八、启动方式

```bash
set ARENA_HERO_API_KEY=your_key
python tactic.py
```

脚本自动以 `DETACHED_PROCESS` 方式后台运行，不受当前 shell 退出影响。如需停止，在任务管理器中结束 `python.exe` 进程。