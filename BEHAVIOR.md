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
- 采集失败提示矿已耗尽（`HARVEST_FAILED / RESOURCE_DEPLETED`）→ 删除
- 工人到达记忆矿点但当前格没有矿 → 删除并在同一 Tick 转入探索
- 前端手动录入的矿点同样会在工人到达并确认无矿后删除

**敌人踪迹记忆（`_enemy_memory`，同样持久化到 map_memory.json）：**
- 任何可见敌人所在的格 → 自动加入踪迹
- 旧踪迹只在**某友方单位确实能看到该格**（该单位自己的视野半径内，且连线无遮挡）
  且该格没有敌人时才清除——工人（视野 3）/先锋（视野 4）路过 4~5 格外的旧踪迹
  不会误删；被墙体挡住视线的踪迹也会保留为"最后一次看到"的线索
- 面板「清除」按钮可随时手动清空全部踪迹

**注意（重要游戏机制）：** 一次采集会把工人载货槽一次性填满（核心携带冠军信标时
容量为 2，采集即得 2）。因此身上已带部分货物的工人**无法再采集**——服务器对任何
继续采集返回 `HARVEST_FAILED / CARGO_FULL`。CARGO_FULL **不代表矿点枯竭**，矿点仍
完好，只是工人只剩 1 格放不下一次采集的量。旧逻辑把 CARGO_FULL 误判为矿枯竭打入
黑名单，反而会把好矿全部拉黑。

**采集规则（按上述机制）：**
- 只有**空载**工人会采集（站在矿点且 `cargo == 0` → HARVEST，一次采满）
- 任何**带载**工人（`cargo > 0`，无论满载或部分）一律回 Core 卸货：
  - 一次采集即满载（0 → 2）→ 满载回城卸货
  - 站在即将枯竭的矿上只采到 1（0 → 1，矿剩余不足一次采集量）→ 部分载货也回城卸掉，
    避免永远卡在 cargo=1 上反复 CARGO_FULL
- 工人带载期间不会被分配新的矿点（分配仅针对空载工人）

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

**地图筛选（按标签显示/隐藏）：** 地图图例中的每个标签都是一个开关按钮，点击即可
显示/隐藏对应类别（核心 / 工人 / 先锋 / 游侠 / 敌人·工人 / 敌人·先锋 / 敌人·游侠 /
敌人·核心 / 敌人(未知) / 敌人踪迹 / 墙 / 可见矿 / 记忆矿 / 路径 / 目标 / 信标 /
进攻目标 / 核心目标 / 手动目标）。敌人按兵种细分，可单独显示/隐藏某一类敌人。
隐藏的类别会变灰并加删除线，点击「全部显示」一键恢复。选择保存在浏览器
localStorage，跨页面刷新与软刷新保留。

### 4.1 Worker（工人）

N 个 Worker（手动生成），优先级从高到低：

| 优先级 | 条件 | 动作 | 说明 |
|--------|------|------|------|
| 1 | 有货 + 在 Core 位置 + 仓库有空间 | **DEPOSIT** | 卸货，资源入库 |
| 2 | 有货 + 不在 Core 位置 | **BFS MOVE** → Core | BFS 多步寻路回基地 |
| 3 | 无货 + 站在矿点上 | **HARVEST** | 采集，一次采满（核心携信标时为 2） |
| 4 | 无货 + 被指派到某个可见矿 | **BFS MOVE** → 指派矿点 | 只去指派给自己的矿 |
| 5 | 无货 + 被指派到某个记忆矿 | **BFS MOVE** → 指派记忆矿 | 导航到已知矿点 |
| 6 | 无货 + 无指派 | **MOVE** UUID 旋转方向探索 | 见下方探索逻辑 |

**关键：任何带货工人（满载或部分）都只回城卸货**，因为一次采集即采满容量，
部分载货（如 cargo=1）的工人永远无法再采（服务器返回 CARGO_FULL）。部分载货通常
来自采到即将枯竭矿点的最后一格（0 → 1），此时直接回城卸掉 1 单位，再回来继续采。

工人到达记忆矿点但未看到矿时，会立即使该记忆失效并按优先级 6 继续探索，避免守在空矿点上永久等待。

**路径规划（A*，接口名仍为 BFS）：**
- 使用 A*（曼哈顿启发），默认最多扩展 2500 个节点，可在仪表盘调整
- 找到短路径，绕过障碍物；比纯 BFS 更适合长距离回城
- 结合死胡同识别，不把路径规划进 凸 字形口袋
- 寻路本 Tick 失败时：载矿工人改用贪心回城（保留目标），不直接改去探索
- 空货工人寻路失败时仍朝目标贪心一步，避免 goal/explore 来回抢

**死胡同识别：**
- 基于已知障碍记忆，识别只有 1 个开口（三面有墙）的空格，即凸字形死胡同
- 迭代收缩：只通向死胡同的单格走廊也会被标为死路
- 探索 / 侦察 / Core 移动默认不进入这些格子
- 若目标（矿点、核心、敌人）本身在死胡同里，允许进入该连通口袋
- 单位已经困在死胡同中时，仍可走出口离开

**探索逻辑（UUID 旋转方向）：**
- 每个工人有唯一 UUID
- `hash(UUID) % 4` 决定方向列表的旋转起点
- 方向列表 `[UP, RIGHT, DOWN, LEFT]` 按 UUID 旋转
- 例如 UUID 哈希=0 → `[UP, RIGHT, DOWN, LEFT]`
- 例如 UUID 哈希=2 → `[DOWN, LEFT, UP, RIGHT]`
- 每个工人自然分散到不同方向
- 探索时优先跳过已识别的死胡同

**回溯避免：**
- 每个工人记录上一帧的坐标 `_worker_last_pos`
- 所有方向按**死胡同惩罚 + 离目标距离 + 回溯惩罚**排序
- 回溯惩罚默认 = 10（足够小到无路可走时仍能回头），可在仪表盘调整或关闭回头规避
- 避免工人在两个格子间来回振荡

**遇敌回避（保持移动免伤）：**
- 游戏机制（玩家确认）：**只要单位保持移动就不会被打到**，只有原地不动的目标会被攻击命中
- 因此当可见敌人在工人的**回避半径**（`enemy_threat_radius`，默认 3，0 表示关闭）内时，
  该工人**每 Tick 必须移动**——绝不允许 HARVEST / DEPOSIT / WAIT，改为朝远离敌人方向移动一格
- 选择移动格时优先：离敌人最远 > 非死胡同 > 不回朔 > 未最近访问；载货工人同等条件下略微偏向核心方向
- 工人不会踩上敌人所在格（敌人格子始终视为阻挡）
- 移动期间被中断的目标/路径会自动丢弃，敌人离开半径后按正常优先级重新规划（继续采矿或回城卸货）
- 回避只针对**可见敌人**；记忆中的旧目击点不触发逃跑，避免工人被早已离开的敌人吓得到处乱跑

### 4.2 战斗分队（先锋 / 游侠）

先锋与游侠通过策略配置编入三支队伍。名单使用仪表盘上的稳定单位名
（`V1`、`R2` 等），逗号/空格/分号分隔。同一单位出现在多队时优先级为：
**守家 > 进攻 > 游击**。

**自动入队：** 新生产出来的先锋/游侠（以及任何仍未编队的战斗单位）会在下一个 Tick
自动写入 `home_team` 并热保存到 `tactic_config.json`。已在进攻队或游击队中的单位不会被改派。
仪表盘刷新后可看到名单更新；也可手动把名字改到进攻/游击队。

#### 守家队（home）

| 优先级 | 条件 | 动作 | 说明 |
|--------|------|------|------|
| 1 | 可立即攻击 | **SWEEP** / **SHOOT** | 先锋扫邻格，游侠打射程内目标 |
| 2 | 敌人进入核心附近 `radius+1` | **MOVE** → 敌人 | 只拦截靠近家的威胁 |
| 3 | 自身超出巡逻半径 | **MOVE** → 核心 | 回防 |
| 4 | 在半径内无威胁 | **MOVE** 到分散巡逻点 | 半径 N 可配置，按 UUID 分 8 个方位 |

#### 进攻队（attack）

进攻队的目标由**进攻方式**（三选一）决定，切换方式后下个 Tick 生效：

| 进攻方式 | 目标 | 说明 |
|----------|------|------|
| **进攻坐标** | `attack_target_x/y` | 集体朝配置坐标推进 |
| **自动进攻** | 记忆中的最近敌人 | 朝最近敌方目击点狩猎 |
| **进攻冠军信标** | 冠军信标当前位置 | 全队朝信标推进，沿途遇敌即战 |

选择“进攻冠军信标”后，进攻坐标与自动进攻设置失效；信标位置始终公开，无需目视。

| 优先级 | 条件 | 动作 | 说明 |
|--------|------|------|------|
| 1 | 可立即攻击 | **SWEEP** / **SHOOT** | 中途主动出击 |
| 2 | 有可见敌人 | **MOVE** → 最近敌人 | 集体接战 |
| 3 | 无敌人 | **MOVE** → 进攻方式目标 | 坐标 / 最近敌人 / 信标 |
| 4 | 已到达目标 | **WAIT** | 原地待命 |

#### 游击队（guerrilla）

| 优先级 | 条件 | 动作 | 说明 |
|--------|------|------|------|
| 1 | 可见敌人 ≥ 3 | **MOVE** 远离敌群中心 | 撤退，不硬刚 |
| 2 | 可见敌人 = 1 且可攻击 | **SWEEP** / **SHOOT** | 单挑 |
| 3 | 可见敌人 = 1 | **MOVE** → 该敌人 | 主动进攻 |
| 4 | 可见敌人 = 2 且可攻击 | **SWEEP** / **SHOOT** | 可打就打，不追 |
| 5 | 其他 | **MOVE** 固定 8 向漫游 | 每单位按 UUID 分到 N/NE/E/SE/S/SW/W/NW 一路；对角用交替四向近似 |

**设计目的：** 守家保核心，进攻推进目标点，游击分散侦察并挑软柿子。

---

## 五、Core（总部）行为

Core 按仪表盘配置的**按兵种需求目标**创建单位：当前数量低于目标就生产，达到目标就停止，超出目标就让一个该兵种自裁；兵阵亡后自动补到目标。

| 优先级 | 条件 | 动作 | 说明 |
|--------|------|------|------|
| 1 | 脚下有 Beacon（地面） | **PICKUP_BEACON** | 捡取冠军信标 |
| 2 | 某兵种低于目标 + 资源满足 + Core 格空闲 | **SPAWN** | 按 工人→先锋→游侠 优先级补兵 |
| 3 | 盾低于设定目标 + 资源 ≥ 1 | **REPAIR_SHIELD** | 1 资源修 1 盾 |
| 4 | 有带矿工人在设定距离内 | **WAIT** | 默认 5 格，停等工人卸货 |
| 5 | 无带矿工人 | **START_MOVE** → 目标方向 | 朝工人中心或最近矿点移动 |

**生产需求目标：**
- 在策略配置面板填写工人 / 先锋 / 游侠目标（各 0–19）
- 当前数量 < 目标 → 自动生产该兵种（工人消耗 5，先锋 10，游侠 12 资源），每 Tick 生产 1 个
- 当前数量 == 目标 → 停止生产
- 当前数量 > 目标 → 每 Tick 让该兵种 1 个单位自裁，直到回到目标（优先自裁空载、离 Core 最远的单位；携带冠军信标的单位永不自裁）
- 兵死亡后会自动补回目标；资源不足或 Core 格被占用时暂停等待
- 总人口达到 `population_cap` 时暂停一切生产

**移动方向计算：**
- 计算所有工人平均位置
- 计算最近矿点（可见 + 记忆）
- 选两者中离 Core 更近的作为目标
- 优先尝试主方向，被挡则尝试其他方向

**移动节奏：** Core 每 4 Tick 走 1 格，迁移期间不能接收卸货。

---

## 六、策略配置与战斗分队

### 策略配置

仪表盘地图下方的“策略配置”面板可调整：

- 工人与寻路：BFS 开关、搜索节点上限、回头规避、载矿回头惩罚、**遇敌回避半径**（0 关闭）
- 核心：移动、矿点偏好、等待距离、修盾开关及和平/战斗修盾目标、**生产保留金币**
  - 生产保留金币：队列中的单位只有在 `资源 ≥ 成本 + 保留` 时才生产
  - 例如保留 20 时，工人需 25 资源，先锋需 30，游侠需 32
- 运行：地图记忆保存间隔
- 生产需求队列：工人 / 先锋 / 游侠

### 战斗分队卡片

战斗分队从策略配置中独立出来，位于地图和策略配置之间：

- 四个拖拽列：**待命池 / 守家队 / 进攻队 / 游击队**
- 直接拖动 `V1`、`R2` 等单位芯片换队，无需输入框
- 拖放后自动保存到 `tactic_config.json`，下个 Tick 生效
- 下方保留守家半径、进攻方式（坐标 / 自动进攻 / 进攻冠军信标 三选一）、游侠射程等参数
- 选择“进攻冠军信标”时进攻坐标输入自动置灰，信标模式忽略坐标与自动进攻

配置保存到 `tactic_config.json`。`tactic.py` 每 Tick 检查文件变更，保存后无需重启战术进程；
无配置文件或配置损坏时使用内置默认值。“恢复默认”会写回完整默认配置。
旧版 `vanguard_engage_enabled` / `ranger_engage_enabled` 会被忽略，由分队行为替代。

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

### 战斗日志（`battle_log.jsonl` + 仪表盘「战斗日志」面板）

配置面板下方新增「战斗日志」面板，展示分类日志并按标签筛选：

- **发现**：发现新矿点 / 新敌人踪迹（对记忆的增量）
- **击杀**：参与摧毁敌方单位 / 核心，摧毁敌方核心并缴获资源
- **被击败**：我方单位被击败 / 维护费亏损致死 / 超编自裁 / 核心受击或被摧毁
- **战斗**：游侠击中、先锋横扫命中、我方单位承伤（默认隐藏，可开启）
- **经济**：挖矿 / 卸货 / 信标加成 / 维护费 / 修盾回血 / 生产（默认隐藏）
- **配置**：仪表盘保存配置 / 分队调整 / 恢复默认（由 dashboard 进程写入）
- **异常**：挖矿/卸货/生产/移动/修复失败、射击未命中等

tactic 进程每 Tick 追加发现与解析事件，dashboard 进程在保存配置时追加配置行；两者用
文件锁并发写同一 `battle_log.jsonl`，超过 2MB 自动裁剪保留最新 500 条。筛选选择保存在
浏览器 localStorage。

### 运行结束汇总
```
TACTIC SUMMARY
  Ticks played:     347
  Harvest actions:  42
  Harvest success:  38
  Deposit success:  35
  Move actions:     285
```

### 战报统计（`game_stats.json`）

Tactic 每 Tick 从解析事件聚合累计统计，持久化到 `game_stats.json`（跨重启保留），仪表盘左侧"战报统计"面板展示：

- **经济**：总采集量/次数、总卸货量/次数、采集失败次数、采集效率（每 Tick、每工人、最近窗口）
- **生产**：各兵种累计生产 / 自裁 / 阵亡、生产失败次数（生产数按快照新增单位归属兵种）
- **战斗**：先锋与游侠各自攻击 / 命中次数与命中率、参与击杀敌方单位次数、承伤次数、扫描次数
- **移动**：移动成功 / 失败次数
- **每单位明细**：工人卡片显示累计采矿/卸货量；先锋/游侠卡片显示攻击/命中/命中率（阵亡后显示"已阵亡 · 生前攻 N 中 N"）

数据口径说明：`SHOT_HIT + SHOT_MISSED = 攻击次数`，`SHOT_HIT = 命中次数`；`DESTRUCTION_PARTICIPATION` 服务器不提供击杀者 id，故"参与击杀"仅全局计数；我方单位死亡由单位快照差异检测。

### 游侠预判影子统计

当前真实射击仍瞄准敌人本 Tick 所在格，不改变战斗行为。战术进程按敌方 UUID
保留最近 4 个连续 Tick 的位置；游侠每次开火时计算下一格预测，并在下一 Tick
对照实际位置和 `SHOT_HIT` / `SHOT_MISSED`。`tactic_log.jsonl` 的
`shot_predictions` 保存候选，`shot_prediction_results` 保存对照结果；累计候选、
正确、错误、未知及理论挽回/伤害写入 `game_stats.json` 的 `shot_prediction`。

轨迹断档会重置；只有连续两步同方向、每步一个基数格且预测格满足射程、八向直线
和障碍检查时才标记为 `eligible`。该标记仅用于采样，不会传入 SDK 的
`expected_cell`。

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
| `_enemy_memory` | `set[tuple[int,int]]` | 跨 Tick 持久化的敌人踪迹（最近目击点） |
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
