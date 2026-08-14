# ArenaGame

<p><strong>Arena Hero</strong> 自动战术机器人 + 实时 Web 仪表盘。</p>
<p>
  <img alt="Python" src="https://img.shields.io/badge/python-3.12-blue">
  <img alt="arena-hero" src="https://img.shields.io/badge/arena--hero-%E2%89%A50.2.9-green">
  <img alt="dashboard port" src="https://img.shields.io/badge/dashboard%20port-4399-orange">
</p>

一个事件驱动的自动战术脚本，通过 <code>arena-hero</code> SDK 以 WebSocket 长连接加入 Arena Hero 对局，每个 Tick 接收完整状态、决策后提交计划；同时自带一个深色中文 Web 仪表盘，可实时查看地图、单位、资源、战斗日志、历史趋势，并在运行中热调整全部策略参数。

- ⚡ **事件驱动**：WebSocket 长连接，非轮询；每 Tick 收到状态后即决策提交
- 🗺️ **实时仪表盘**：平移缩放 SVG 地图 + 分队卡片 + 趋势图 + 分类战斗日志
- 🔥 **热加载配置**：仪表盘保存的配置在下一个 Tick 即时生效，无需重启战术进程
- 🧠 **持久记忆**：矿点、敌人踪迹、战报统计跨 Tick、跨重启保留
- 🐳 **单容器部署**：战术进程与仪表盘同容器运行，状态落在 Docker volume 上
- 🔐 **令牌鉴权**：非环回请求必须携带 <code>DASHBOARD_TOKEN</code>

---

## 目录

- [它是做什么的](#它是做什么的)
- [架构总览](#架构总览)
- [项目结构](#项目结构)
- [快速开始](#快速开始)
- [配置](#配置)
- [战术行为](#战术行为)
- [Web 仪表盘](#web-仪表盘)
- [日志与统计](#日志与统计)
- [Docker 部署](#docker-部署)
- [单 VPS 部署](#单-vps-部署)
- [测试](#测试)
- [环境变量](#环境变量)
- [技术细节](#技术细节)
- [许可证](#许可证)

---

## 它是做什么的

ArenaGame 是一个 Arena Hero 自动化玩家。它代替你：

1. **指挥经济**：自动生产工人、分配矿点、调度采集与卸货，避免蜂拥同一个矿
2. **指挥战斗**：把先锋/游侠编入守家、进攻、游击三支队伍，各自执行拦截、推进、骚扰
3. **运营核心**：按可配置的兵种目标自动补兵、修盾、必要时迁移核心
4. **实时反馈**：通过浏览器仪表盘观察对局地图、各单位行为、资源和人口趋势、战斗事件，并随时调整策略

整个系统由两个进程组成：<strong>tactic</strong>（决策大脑）和 <strong>dashboard</strong>（HTTP UI/API），二者通过共享的本地文件（JSONL 日志、配置、地图记忆、战报）松耦合通信。

---

## 架构总览

```
server ──WebSocket state──▶ SDK
        ──Turn──▶ tactic.choose_actions() ──plan──▶ SDK ──HTTP POST──▶ server
                                │
                                ├─ 写 tactic_log.jsonl (每 Tick 完整状态)
                                ├─ 写 battle_log.jsonl  (分类战斗事件)
                                ├─ 写 map_memory.json   (矿点/敌人踪迹)
                                ├─ 写 game_stats.json   (累计战报)
                                └─ 读 tactic_config.json (每 Tick 热加载)
                                        ▲
                                        │ 保存
dashboard.py ◀── HTTP ── 浏览器
   读：tactic_log.jsonl / map_memory.json / game_stats.json / battle_log.jsonl
   写：tactic_config.json / battle_log.jsonl(config 行) / waypoints.json
```

**关键设计：**

- 决策完全基于当前 Tick 的 `state`，不跨 Tick 复用控制器对象
- 仪表盘保存的配置由 tactic 进程每 Tick 检查文件变更并热加载
- tactic 与 dashboard 用跨进程文件锁（`state_io.file_lock`）并发写同一份 `battle_log.jsonl`
- 所有可变状态通过 `ARENA_DATA_DIR` 指向同一目录，Docker 挂载到 volume

---

## 项目结构

```
ArenaGame/
├── tactic.py              # 主战术脚本（核心决策逻辑，事件驱动）
├── tactic_config.py       # 策略配置字段定义、校验、热加载与原子写
├── tactic_config.json     # 运行时配置（仪表盘生成，git 忽略）
├── dashboard.py           # Web 仪表盘 HTTP 服务 + 单文件前端
├── game_stats.py          # 累计战报统计聚合与持久化
├── state_io.py            # 跨进程文件锁、原子写、JSONL 追加与裁剪
├── status.py              # 命令行实时状态查看器
├── direct_wrapper.py      # 直接控制桥接器（备用，带自动重启）
├── watchdog.py            # 看门狗：守护 direct_wrapper 不挂、不卡
├── docker-entrypoint.py   # 容器入口：启动 tactic + dashboard 并守护
├── diagnose.py            # 诊断辅助脚本
├── requirements.txt       # 依赖：arena-hero>=0.2.9,<0.3
├── Dockerfile             # 单容器镜像（python:3.12-slim）
├── compose.yml            # 单服务编排 + volume + healthcheck
├── .env.example           # 本地运行环境变量模板
├── BEHAVIOR.md            # 战术行为逻辑完整说明（中文）
└── deploy/                # VPS 一键部署脚本
    ├── deploy.py           #   SFTP + docker compose 一键部署
    ├── deploy_source_only.py
    ├── .env.deploy.example #   部署凭据模板
    └── DEPLOYMENT.md       #   部署手册
```

**运行时产物（git 忽略）：**

| 文件 | 写入者 | 内容 |
|------|--------|------|
| `tactic_log.jsonl` | tactic | 每 Tick 完整状态快照（可达数十 MB，自动轮转） |
| `battle_log.jsonl` | tactic + dashboard | 分类战斗/配置事件（超 2 MB 自动裁剪保留 500 条） |
| `map_memory.json` | tactic | 矿点坐标 + 敌人踪迹（跨重启持久） |
| `game_stats.json` | tactic | 累计经济/生产/战斗统计（跨重启持久） |
| `waypoints.json` | dashboard | 仪表盘手动设定的单位目标点 |
| `tactic_play.log` | tactic | 控制台输出记录 |

---

## 快速开始

### 前置要求

- Python 3.12+（Docker 镜像即 3.12-slim）
- 一份有效的 Arena Hero API key
- （可选）Docker Engine + Compose 插件，用于容器化运行

### 本地直接运行

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置环境变量
cp .env.example .env
#  编辑 .env，填入 ARENA_HERO_API_KEY 和 DASHBOARD_TOKEN

# 3. 启动战术进程（前台，Ctrl+C 会保存地图记忆并退出）
python tactic.py

# 4. 另开一个终端，启动仪表盘
python dashboard.py
#  打开 http://localhost:4399，填入 DASHBOARD_TOKEN 登录
```

Windows PowerShell 等价写法：

```powershell
$env:ARENA_HERO_API_KEY = "your_key"
$env:DASHBOARD_TOKEN    = "your_token"
python .\tactic.py      # 终端 1
python .\dashboard.py   # 终端 2
```

### 命令行状态查看

无需浏览器即可查看最新 Tick 的解耦状态：

```bash
python status.py
```

输出包含核心动作、可见资源、敌人数量、每个工人/先锋/游侠的位置与动作，以及卡死/振荡检测。

---

## 配置

所有可调策略参数集中在 `tactic_config.py` 的 `CONFIG_FIELDS`，分为五组：

| 组 | 字段示例 | 说明 |
|----|----------|------|
| **工人与寻路** | `worker_bfs_enabled`, `bfs_max_steps`, `avoid_backtracking`, `backtrack_penalty`, `enemy_threat_radius`, `worker_mine_max_distance` | A* 寻路开关、节点上限、回头规避、遇敌回避半径 |
| **核心** | `core_movement_enabled`, `cargo_wait_distance`, `repair_enabled`, `heal_enabled`, `peace_shield_target`, `combat_shield_target`, `resource_reserve` | 核心移动、修盾目标、生产保留金币 |
| **战斗分队** | `home_team`, `attack_team`, `guerrilla_team`, `home_patrol_radius`, `home_engage_memory_ticks`, `attack_target_x/y`, `attack_mode`, `guerrilla_engage_radius`, `ranger_attack_range`, `ranger_lead_fire_enabled` | 三队名单（V1/R2 等）、守家/进攻/游击策略、游侠射程与预判 |
| **运行** | `map_save_interval_ticks` | 地图记忆落盘节奏 |
| **生产** | `target_workers`, `target_vanguards`, `target_rangers` | 各兵种目标数量（0–100） |

配置在仪表盘分区修改：战斗相关字段位于“战斗分队”，生产、工人、核心和运行字段位于“策略配置”。保存即写入 `tactic_config.json`，tactic 进程下个 Tick 自动热加载。也可通过 API 直接更新：

```bash
# 更新部分字段（合入最新磁盘配置）
curl -X POST http://localhost:4399/api/config \
  -H "Authorization: Bearer $DASHBOARD_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"target_workers": 12, "enemy_threat_radius": 4}'

# 恢复全部默认
curl -X POST http://localhost:4399/api/config/reset \
  -H "Authorization: Bearer $DASHBOARD_TOKEN"
```

校验失败会返回 400 + 每个错误字段的原因；未设 `DASHBOARD_TOKEN` 时本地环回免鉴权。

---

## 战术行为

完整行为说明见 [`BEHAVIOR.md`](BEHAVIOR.md)，此处为摘要。

### 资源记忆与分配

- `_resource_memory`：跨 Tick 持久化的已知矿点坐标集，写到 `map_memory.json`
- `_enemy_memory`：跨 Tick 持久化的敌人踪迹（最近目击点），仅当友方单位确实能看到该格且格上无敌人时才清除
- `_resource_assignments`：每 Tick 重算，按最近距离把矿点分配给最近且未分配的工人，防止全队蜂拥同一矿

> **关键游戏机制**：一次采集会把工人载货槽一次性填满（携带冠军信标时容量为 2）。因此身上已带部分货物的工人**无法再采集**——服务器返回 `CARGO_FULL`，但这**不代表矿点枯竭**。脚本据此只让**空载**工人采集，任何带载工人一律回 Core 卸货。

### 单位优先级

**工人（Worker）**

| 优先级 | 条件 | 动作 |
|--------|------|------|
| 1 | 有货 + 在 Core + 仓库有空间 | DEPOSIT |
| 2 | 有货 + 不在 Core | BFS MOVE → Core |
| 3 | 无货 + 站矿点 | HARVEST（一次采满） |
| 4 | 无货 + 指派到可见矿 | BFS MOVE → 矿点 |
| 5 | 无货 + 指派到记忆矿 | BFS MOVE → 记忆矿 |
| 6 | 无货 + 无指派 | MOVE 按 UUID 旋转方向探索 |

**战斗分队**（先锋 V / 游侠 R，按稳定单位名编队，同一单位多队时优先级 守家 > 进攻 > 游击）

- **守家队**：拦截靠近核心的威胁，超过巡逻半径则回防，无威胁时按 UUID 分 8 方位分散巡逻
- **进攻队**：可三选一目标——进攻坐标 / 自动狩猎最近记忆敌人 / 直扑冠军信标；沿途遇敌即战
- **游击队**：见 3+ 敌人撤退，单敌单挑，2 敌能打则打不追，其余按 UUID 8 向漫游

> **游戏机制**：只要单位保持移动就不会被命中，原地才会被打。因此当可见敌人在工人的 `enemy_threat_radius` 内时，工人**每 Tick 必须移动**，禁止 HARVEST/DEPOSIT/WAIT，改为朝远离敌人方向移动一格。

**核心（Core）** 按 `cargo_wait_distance` 默认 5 格内停等带矿工人卸货，无带矿工人时朝工人中心或最近矿点迁移（每 4 Tick 1 格）；按兵种目标数量生产（不足则补，达目标停止，超出不再自裁）。

### 寻路与死胡同

- 路径规划用 A*（曼哈顿启发，接口名仍为 BFS），默认最多扩展 2500 节点，可在仪表盘调整
- 死胡同识别：标记三面有墙的凸字形口袋并迭代收缩单格走廊，探索/侦察/Core 移动默认不进入这些格子（除非目标本身在其中）
- 回溯惩罚（默认 10）：避免工人在两格间振荡，又小到无路可走时仍能回头
- 寻路失败时载矿工人改用贪心回城（保留目标），空货工人仍朝目标贪心一步避免 goal/explore 抢路

### 游侠稳定移动预判

战术进程按敌方 UUID 保留最近 4 个连续 Tick 的位置，每次开火时预测目标下一格，并在下一 Tick 对照 `SHOT_HIT`/`SHOT_MISSED`。只有连续三步同方向、每步一基数格的「稳定移动」目标且预测格满足射程/八向直线/障碍检查时，才标记为 `eligible` 并把预测格传给 SDK 的 `expected_cell`。累计候选/正确/错误/未知及理论挽回伤害写入 `game_stats.json` 的 `shot_prediction`。`ranger_lead_fire_enabled` 默认开启，面板可随时关闭恢复纯影子模式。

---

## Web 仪表盘

`dashboard.py` 是一个零依赖前端框架的单文件 Web 应用（HTTP 服务 + 内嵌 HTML/CSS/JS），默认端口 **4399**。

### 主要面板

- **地图舞台**：平移缩放 SVG，显示核心、工人、先锋、游侠、敌人（按兵种细分）、敌人踪迹、墙、可见/记忆矿、单位路径、目标、信标等。图例标签均为可点击开关，显示/隐藏对应类别，选择持久化在 localStorage
- **战斗分队卡片**：四个拖拽列（待命池 / 守家 / 进攻 / 游击）和守家、进攻、游击、通用四组参数；拖拽与参数修改先形成草稿，点击“保存修改”后统一生效。进攻方式只显示当前模式需要的字段
- **策略配置面板**：生产需求及工人、核心、运行参数，含范围校验；不会隐式修改战斗分队
- **战报统计面板**：经济（采集/卸货/效率）、生产（各兵种生产/自裁/阵亡）、战斗（攻击/命中/命中率/参与击杀）、移动，每单位明细
- **历史趋势**：资源、人口、敌人三图，窗口按时间可选（最近 10 分钟 / 30 分钟 / 1 小时），横轴按真实墙钟时间排布，选择持久化
- **战斗日志面板**：分类日志（发现 / 击杀 / 被击败 / 战斗 / 经济 / 配置 / 异常）按标签筛选，可加时间窗口（最近 10 分钟 / 30 分钟 / 1 小时 / 6 小时 / 自定义分钟数 / 全部），选择持久化

### 主要 API 路由

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/login` | 用 token 换 cookie（无须鉴权） |
| GET  | `/api/state` | 最新 Tick 完整状态 JSON |
| GET  | `/api/config` | 当前策略配置 |
| POST | `/api/config` | 合并更新配置（原子写 + 校验） |
| POST | `/api/config/reset` | 恢复全部默认 |
| GET  | `/api/teams` | 分队配置 + 可编队战斗单位 |
| POST | `/api/teams` | 更新分队名单与设置 |
| GET  | `/api/trends?seconds=N` | 最近 N 秒内的趋势数据点序列（时间过滤） |
| GET/POST | `/api/waypoints` | 手动单位目标点 |

### 鉴权

设置 `DASHBOARD_TOKEN` 后，非环回请求必须携带该 token（cookie / `Authorization: Bearer` / `?token=`）。来自 `127.0.0.1` 的环回请求始终放行，便于容器 healthcheck 与部署冒烟测试。未设 token 时鉴权关闭（仅本地开发用）。

---

## 日志与统计

### 控制台输出（每 Tick 一行）
```
tick=35060 core=MOVE_RIGHT res=9/45 pop=9 workers=7 enemies=0 resources_visible=1 memory=1
```

### JSONL 日志 `tactic_log.jsonl`
每 Tick 完整状态：Tick、时间戳、Core 状态、资源/容量/人口/生产单价、所有单位位置货物 HP、动作指令、目标与规划路径、可见敌人、矿点、上 Tick 解析事件、决策与提交耗时。文件超 `ARENA_LOG_MAX_MB`（默认 20）自动轮转，最多保留 3 个备份。

### 战斗日志 `battle_log.jsonl`
分类事件，由 tactic（发现/解析事件）与 dashboard（配置/分队改动）并发写入，用 `file_lock` 串行化，超 2 MB 自动裁剪保留最新 500 条。

### 战报统计 `game_stats.json`
每 Tick 从解析事件聚合累计：经济、生产、战斗命中率、移动、游侠预判准确度，跨重启保留，面板渲染。每单位明细按稳定名跟踪（阵亡后显示「已阵亡 · 生前攻 N 中 N」）。

---

## Docker 部署

仓库自带 `Dockerfile` 与 `compose.yml`，把 tactic + dashboard 跑进同一容器，可变状态落在 `arena-game-runtime` volume。

```bash
# 1. 准备 .env（同 .env.example）
cp .env.example .env
# 填入 ARENA_HERO_API_KEY、DASHBOARD_TOKEN

# 2. 构建并后台启动
docker compose up --build --detach
docker compose ps            # 应见 0.0.0.0:4399->4399/tcp
docker compose logs --follow app

# 3. 访问（非环回需带 token）
curl -i http://localhost:4399/ -H "Authorization: Bearer $DASHBOARD_TOKEN"
```

容器特性：

- 非 root 用户 `arena`（uid 10001）运行
- `docker-entrypoint.py` 守护 tactic 进程（崩溃自动重启）与 dashboard 进程
- healthcheck 命中环回 `/api/state`
- `stop_grace_period: 20s` 给 tactic 留时间保存地图记忆
- runtime volume 备份与恢复见 [单 VPS 部署](#单-vps-部署)

---

## 单 VPS 部署

`deploy/` 下提供一键 SFTP + docker compose 部署。完整步骤见 [`deploy/DEPLOYMENT.md`](deploy/DEPLOYMENT.md)。

```bash
cp deploy/.env.deploy.example deploy/.env.deploy
# 填入 DEPLOY_HOST / DEPLOY_USER / DEPLOY_PASSWORD / ARENA_HERO_API_KEY / DASHBOARD_TOKEN

pip install paramiko
python deploy/deploy.py
```

脚本会：SFTP 上传项目文件 → 远端写入 `.env` → `docker compose up --build --detach` → 冒烟测试 `/` 与 `/api/state`。

**安全须知**：部署使用未加密 HTTP，全靠 `DASHBOARD_TOKEN` 把关（连查看仪表盘都需要令牌）。强烈建议在云防火墙/安全组把 TCP 4399 限制到可信来源 IP 或 VPN。

---

## 测试

```bash
python -m pytest tests/
```

- `tests/test_tactic_config.py`：配置字段、校验、迁移、热加载
- `tests/test_regressions.py`：行为回归（约 165 个用例，覆盖寻路、死胡同、采集、卸货、回避、分队等）

---

## 环境变量

| 变量 | 必需 | 说明 |
|------|------|------|
| `ARENA_HERO_API_KEY` | ✅ | Arena Hero API key（vps 上若未设，容器跳过 tactic 仅起 dashboard） |
| `DASHBOARD_TOKEN` | ✅* | 仪表盘鉴权令牌；空则鉴权关闭，仅本地用 |
| `ARENA_DATA_DIR` | 否 | 可变状态目录（Docker 指向 `/app/runtime` volume） |
| `ARENA_LOG_MAX_MB` | 否 | 日志轮转阈值 MB，默认 20 |
| `LOG_LEVEL` | 否 | 日志级别，默认 info |
| `HOST` / `PORT` | 否 | 仪表盘监听，默认 `0.0.0.0:4399` |

部署专用（`deploy/.env.deploy`）：`DEPLOY_HOST`、`DEPLOY_PORT`、`DEPLOY_USER`、`DEPLOY_PASSWORD`、`DEPLOY_REMOTE_BASE`、`APP_PORT`。

---

## 技术细节

- **零前端框架**：`dashboard.py` 用 Python `http.server` 起服务，前端 HTML/CSS/JS 全部内联字符串，无 npm 构建步骤
- **跨进程文件锁 `state_io.file_lock`**：Linux 用 `fcntl.flock`，Windows 用 `msvcrt.locking`；tactic 与 dashboard 并发写 `battle_log.jsonl` / `tactic_config.json` 时串行化
- **原子写 `atomic_write_text`**：临时同名文件 + `os.replace`，避免写一半的配置被读到
- **流式反向读日志 `_iter_log_lines_reverse`**：按块从尾部读 JSONL，不在内存装载整个（可达数十 MB 的）日志，dashboard 与 `status.py` 共用
- **稳定对象命名**：核心 `C1`、工人 `W1...`、先锋 `V1...`、游侠 `R1...`、敌人 `E1...`，按首次出现顺序与 UUID 绑定，不随列表顺序变化；地图里不同颜色绘制各工人路线（实线=完整 BFS，虚线=单步回退/短期规划，圆环=目标格）

---

## 许可证

仓库目前未附带许可证文件；如需复用代码请自行与作者确认授权。`arena-hero` SDK 的许可以其实际条款为准。
