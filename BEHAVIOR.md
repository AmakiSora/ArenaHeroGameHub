# Arena Hero 战术脚本行为逻辑

## 概述

`tactic.py` 是一个 WebSocket 事件驱动的自动战术脚本，通过 `arena-hero` SDK 连接游戏服务器，每 Tick 接收一次完整状态，决策后提交计划。

- 非轮询，WebSocket 长连接，事件驱动
- 每 Tick 收到 `Turn` 后立即决策提交
- 所有决策基于当前 Tick 的 `state`，不跨 Tick 复用控制器
- 仪表盘保存的策略配置会在下一个 Tick 热加载

---

## 一、总体架构

```
server --WebSocket state--> SDK --Turn--> choose_actions() --plan--> SDK --HTTP POST--> server
```

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
- 无货工人优先去**指派给自己的矿点**（见下方资源分配机制）

---

## 三、资源分配机制（防蜂拥）

### `_resource_assignments: dict[str, tuple[int, int]]`

每 Tick 重新计算，防止所有工人冲同一个矿点。

**分配算法：**
1. 合并所有可见矿点与记忆矿点，并按坐标去重
2. 按**最近距离**排序矿点
3. 每个矿点分配给**最近且未分配**的工人
4. 一个工人最多分到一个矿点
5. 工人多于矿点时，多余工人去探索

**效果：**
- 7 个工人、3 个矿 → 3 个工人各去一个矿，4 个工人探索
- 不会出现 7 个人全挤在同一个矿点的情况

---

## 四、单位行为

### 4.0 对象命名与路线

所有主动对象按首次出现顺序获得稳定名称：核心 `C1`、工人 `W1...`、先锋 `V1...`、
游侠 `R1...`、可见敌人 `E1...`。名称在同一战术进程内与 UUID 绑定，不会随列表顺序变化。

每个工人的当前坐标、目标坐标和本 Tick 规划路径会写入日志。仪表盘地图使用不同颜色绘制各工人路线：
完整 BFS 路径使用实线，BFS 失败后的单步回退或关闭 BFS 时的短期规划使用虚线，圆环表示目标格。

### 4.1 Worker（工人）

N 个 Worker（手动生成），优先级从高到低：

| 优先级 | 条件 | 动作 | 说明 |
|--------|------|------|------|
| 1 | 有货 + 在 Core 位置 + 仓库有空间 | **DEPOSIT** | 卸货，资源入库 |
| 2 | 有货 + 不在 Core 位置 | **BFS MOVE** → Core | BFS 多步寻路回基地 |
| 3 | 无货 + 站在矿点上 | **HARVEST** | 采集，1 次采 1 单位 |
| 4 | 无货 + 被指派到某个可见矿 | **BFS MOVE** → 指派矿点 | 只去指派给自己的矿 |
| 5 | 无货 + 被指派到某个记忆矿 | **BFS MOVE** → 指派记忆矿 | 导航到已知矿点 |
| 6 | 无货 + 无指派 | **MOVE** UUID 旋转方向探索 | 见下方探索逻辑 |

**BFS 路径规划：**
- 使用广度优先搜索（BFS），默认最多探索 800 个节点，可在仪表盘调整
- 找到最短路径，绕过障碍物
- 避免走入死胡同
- BFS 无路可走时 → 切换到探索模式（不原地打转）

**探索逻辑（UUID 旋转方向）：**
- 每个工人有唯一 UUID
- `hash(UUID) % 4` 决定方向列表的旋转起点
- 方向列表 `[UP, RIGHT, DOWN, LEFT]` 按 UUID 旋转
- 例如 UUID 哈希=0 → `[UP, RIGHT, DOWN, LEFT]`
- 例如 UUID 哈希=2 → `[DOWN, LEFT, UP, RIGHT]`
- 每个工人自然分散到不同方向

**回溯避免：**
- 每个工人记录上一帧的坐标 `_worker_last_pos`
- 所有方向按**离目标距离 + 回溯惩罚**排序
- 回溯惩罚默认 = 10（足够小到无路可走时仍能回头），可在仪表盘调整或关闭回头规避
- 避免工人在两个格子间来回振荡

### 4.2 Vanguard（先锋）

1 个 Vanguard（手动生成），优先级：

| 优先级 | 条件 | 动作 | 说明 |
|--------|------|------|------|
| 1 | 相邻格有敌人 | **SWEEP** | 对相邻格扇形攻击，1 伤害 |
| 2 | 有可见敌人但不相邻 | **MOVE** → 最近敌人 | 靠近敌人 |
| 3 | 无敌人 | **MOVE** UUID 旋转方向巡游 | 同 Worker 探索逻辑 |

**设计目的：** 先锋探路，发现敌人后主动接战，掩护工人。

### 4.3 Ranger（游侠）

1 个 Ranger（手动生成），优先级：

| 优先级 | 条件 | 动作 | 说明 |
|--------|------|------|------|
| 1 | 敌人在设定射程内 + 无障碍 | **SHOOT** | 默认最远 3 格，远程攻击 1 伤害 |
| 2 | 有可见敌人但超出射程 | **MOVE** → 最近敌人 | 进入射程 |
| 3 | 无敌人 | **MOVE** UUID 旋转方向巡游 | 同 Worker 探索逻辑 |

**设计目的：** 游侠远程支援，视野 5 格比 Vanguard 还远，没敌人时也巡游。

---

## 五、Core（总部）行为

Core 不自动生产单位，完全手动控制。

| 优先级 | 条件 | 动作 | 说明 |
|--------|------|------|------|
| 1 | 脚下有 Beacon（地面） | **PICKUP_BEACON** | 捡取冠军信标 |
| 2 | 盾低于设定目标 + 资源 ≥ 1 | **REPAIR_SHIELD** | 1 资源修 1 盾 |
| 3 | 有带矿工人在设定距离内 | **WAIT** | 默认 5 格，停等工人卸货 |
| 4 | 无带矿工人 | **START_MOVE** → 目标方向 | 朝工人中心或最近矿点移动 |

**移动方向计算：**
- 计算所有工人平均位置
- 计算最近矿点（可见 + 记忆）
- 选两者中离 Core 更近的作为目标
- 优先尝试主方向，被挡则尝试其他方向

**移动节奏：** Core 每 4 Tick 走 1 格，迁移期间不能接收卸货。

---

## 六、策略配置

仪表盘地图下方的“策略配置”面板可调整 14 项参数：

- 工人与寻路：BFS 开关、搜索节点上限、回头规避和载矿回头惩罚
- 核心：移动、矿点偏好、等待距离、修盾开关及和平/战斗修盾目标
- 战斗：先锋和游侠是否主动接战、游侠开火距离
- 运行：地图记忆保存间隔

配置保存到 `tactic_config.json`。`tactic.py` 每 Tick 检查文件变更，保存后无需重启战术进程；
无配置文件或配置损坏时使用内置默认值。“恢复默认”会写回完整默认配置。

---

## 七、日志系统

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
- 所有主动对象的顺序名称及可见敌人的位置
- 每个工人的目标坐标、规划路径和路径完整状态
- 可见敌人数量、可见矿点、记忆矿点数
- 每单位发出的指令
- 上 Tick 的解析事件（采集成功/失败、卸货成功等）
- 决策与提交总耗时

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

## 八、Git 提交历史

```
aa51085 feat: assign each resource to closest worker, others explore instead of stampede
db470da feat: BFS pathfinding for goal-directed movement, breaks out of dead ends
32574b7 fix: reduce backtrack penalty from 100 to 10 to avoid dead-end lock
544e355 fix: add backtrack avoidance to goal-directed movement too
f851e5e feat: avoid backtracking in exploration - each worker remembers last position
5d79929 docs: update BEHAVIOR.md - worker count is dynamic
75a2efb docs: add BEHAVIOR.md with complete tactic logic documentation
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

## 九、关键变量

| 变量 | 类型 | 说明 |
|------|------|------|
| `_resource_memory` | `set[tuple[int,int]]` | 跨 Tick 持久化的矿点坐标 |
| `_resource_assignments` | `dict[str, tuple[int,int]]` | 每 Tick 重算的矿→工人分配 |
| `_worker_last_pos` | `dict[str, tuple[int,int]]` | 每个工人上一帧坐标（防回朔） |

---

## 十、文件结构

```
tactic.py          # 主战术脚本（核心逻辑）
tactic_config.py   # 策略配置定义、校验与热加载
tactic_config.json # 仪表盘生成的运行时配置（Git 忽略）
direct_wrapper.py  # 直接控制桥接器（备用，BFS 寻路）
dashboard.py       # Web 战术仪表盘
status.py          # 命令行状态查看器
requirements.txt   # 依赖：arena-hero>=0.2.4,<0.3
BEHAVIOR.md        # 本文件，行为逻辑说明
tactic_log.jsonl   # 运行日志（JSONL 格式，可分析）
tactic_play.log    # 控制台输出记录
```

## 十一、启动方式

```powershell
$env:ARENA_HERO_API_KEY = "your_key"
python .\tactic.py
```

脚本在当前终端前台运行，按 `Ctrl+C` 会保存地图记忆、写入日志汇总并退出。另开终端运行
`python .\dashboard.py`，可通过 `http://localhost:4399` 查看战术仪表盘。
