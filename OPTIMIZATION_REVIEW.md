# ArenaGame 优化评审

> 评审对象：本仓库当前工作树（`master` HEAD `2ec3d6b`）。
> 方法：静态阅读 + 生产快照回放 + 微基准 + 可达性追踪。
> 标注约定：`[实测]` = 在本次评审中跑过基准或统计；`[代码]` = 仅由代码路径推导；`[日志]` = 项目自带文档/巡检记录的既有读数。

---

## 修复状态（2026-11-15）

19 个优化点已修复 18 项，1 项按计划部分落地。所有改动以「行为不变」为前提，测试从 856 → 830（净减的 26 条全部是已删除 SSR 面板的用例，另新增 4 条日志瘦身/路由恢复用例）。

| # | 优化点 | 状态 | 落地结果 |
|---|---|---|---|
| 1 | 环回即已登录 → CSRF + WS 劫持 | ✅ | `Origin`/`Host` 校验（含 WS 升级）、POST 强制 `application/json`、环回旁路只留 `GET /api/health`+`GET /api/state`、安全响应头 + CSP、`Server: arena`、4xx/5xx 访问日志 |
| 2 | `/api/state?full=1` 仍渲染 SSR | ✅ | 分支与 `build_parts`/`render_svg` 一并删除；`/api/state` 恒为 43 字节紧凑探针 |
| 3 | 部署上传 112.8 MB 垃圾 | ✅ | 构建上下文 **115.05 MB → 2.13 MB**；上传 **202 文件 / 2.16 MB**（显式清单 + 2 MB 护栏） |
| 4 | `AutoAddPolicy` + root + 打印 token | ✅ | `deploy/ssh_common.py`：固定 known_hosts + `RejectPolicy`、首次 TOFU 需显式授权、支持密钥登录、全量脱敏、冒烟断言 JSON `ok` |
| 5 | 健康检查/守护发现不了死机器人 | ✅ | `/api/health` 返回 `tick_age_s`/`tactic_alive`，过期 **503**；entrypoint 指数退避 + 崩溃计数 + 超限退出 + 停滞看门狗；`tactic_play.log` 轮转；compose 日志上限 |
| 6 | 每拍全量比较 48.8 万墙 | ✅ | 世代计数器 + O(1) 采样探针：**25.7 ms → 0.0014 ms/拍（≈18,000×）** |
| 7 | A\* 每次重建 81 万格 `blocked` | ✅ | `_BlockedView` 成员视图：**81.7 ms → 5.6 ms/次（14.6×）**；220 组等价性验证 0 差异 |
| 8 | 战斗单位复制墙集 → 4.2 s 批处理 | ✅ | `_ObstacleOverlay` 保持身份缓存；`_dead_end_cells_batch` 加 >10 万墙熔断（**5.5 s → 2.2 ms**） |
| 9 | fork 存盘后父进程重解析 map | ✅ | `_reap_map_save_child` 成功后刷新签名缓存；defer 空转拍 **1 → 0 次加锁** |
| 10 | 每拍日志 53 KB | ✅ | 未变更路线只记 `path: null` + `path_len`，dashboard 反向恢复：**51.8 KB → 11.1 KB（−79%）**，20 MB 轮转容纳 395 → 1,847 拍 |
| 11 | dashboard 33% 死 SSR | ✅ | **3,861 → 2,890 行**（净 −1,037），顺带删除 `/api/log`、`/api/stream`、`/api/trends` 与 3 个死全局 |
| 12 | `tactic.py` 单体与 185 个全局 | ⚠️ 部分 | 已消除「新增临时障碍就复制整张墙集」这一根因（`_ObstacleOverlay`）、`_object_names` 敌人名 LRU 封顶 4096；**`_choose_actions` 拆分留待独立一轮**（10,928 行文件的重构在发布前做风险过高） |
| 13 | 4 份 `_data_dir()` | ✅ | 收敛为 `state_io.data_dir()/data_path()`，5 处调用点统一（tactic / tactic_config / game_stats / dashboard / status） |
| 14 | 零 CI / 零工具链 | ✅ | `pyproject.toml`（ruff `E,F,I,UP,B` + pytest + coverage）、`requirements-dev.txt`、`.github/workflows/ci.yml`（python + web 双 job）、`tests/conftest.py`（运行时文件不再落到仓库根）、README 订正 |
| 15 | 112.8 MB 临时件 + 3 份 tactic 快照 | ✅ | 59 个文件移出仓库到 `..ArenaGame_scratch_archive`；删除 `direct_wrapper.py`（650 行）与 `watchdog.py` Windows 守护（339 → 205 行）；新增 `.gitattributes` 统一 LF |
| 16 | 迷雾层每帧扫视口 | ✅ | 迷雾+路线着色烘焙进离屏瓦片：每帧逐格迭代 **162,711 → 0**（默认缩放 24 次 blit） |
| 17 | 每 tick 8 个轮询 | ✅ | `dashboardRequest` + `useTickLoader`：每 tick 共享一个 `AbortSignal`、页面隐藏不轮询、失败可区分（401 不再等同「没数据」） |
| 18 | 同一 tick 算两遍 A\* | ✅ | 上下文 WeakMap 记忆化 + `applyAutonomousMovement` 回传路线：**2 → 1** 次导航上下文、2 → 0 次 `JSON.stringify` |
| 19 | 899.5 KB 未压缩 PNG | ✅ | 按 ≤2× 绘制尺寸重导出 WebP + 按层懒加载：**921 KB → 69.8 KB（−92.4%）**，`dist` 2.25 MB → 1.41 MB |

**本轮顺带修掉的额外缺陷**（评审时未列出、实现中撞见）：

- `dashboard.py` 的 `HOST`/`PORT` 是硬编码常量，README/compose/Dockerfile 里文档化的同名环境变量**从来没被读取**（改端口会静默失效）。
- `/api/v1/*` 代理把 `urllib` 的 `HTTPError` 泄漏（每个上游 4xx 丢一个 fd，并在 3.14 触发 ResourceWarning）。
- `read_guerrilla_tracks` 的缓存键里含秒级时间戳，60 s 节流从未生效 → 每次请求重读 5 MB JSONL（**冷 91.8 ms → 热 0.7 ms，125×**）。
- `/api/enemy-memory` 为 63 条目击解码并排序 45 万格墙体（**522.8 ms → 72.4 ms，7.2×**）。
- `_battle_log_html` 之外还有 `fmt_pos`、`_enemy_type_char`、`_ENEMY_TYPE_STYLE`、`_wp_count` 等零引用符号。
- `tests/test_regressions.py::RoamOscillationConfigTests::test_shipped_config_file_contains_the_field` 读的是 gitignore 掉的运行时文件 `tactic_config.json`，在干净克隆上必然失败 —— 改为断言出厂默认值 + 一次 save/load 往返。

**验证**：`python -m pytest` 830 passed / 0 failed；`ruff check .` 全绿；`npm run test` 303 passed；`npm run lint`、`tsc -b`、`npm run build` 全绿；本地端到端冒烟（15 项：鉴权矩阵、紧凑探针、SPA+CSP、CSRF/Content-Type 闸门、路由恢复）全通过。

---

## 数据来源

| 来源 | 用途 |
|---|---|
| `_remote_tacticlog_tail.jsonl`（162 拍完整 phase 剖面） | 每拍耗时分布、`obstacle_memory_count`、日志体积 |
| `_remote_map_memory_current.json` / `_remote_state_latest.json` | 生产规模：约 48.8 万（现）～76 万（峰值）格墙体、2.3 万矿点、2,156 条敌人目击 |
| `INSPECTION_LOG.md` | 项目自己的历史实测：`/api/state` 22,100,454 字节 / 冷 4.97s、`map-rewrite/dead-end rebuild 60/60 拍`、tick 延迟 mean 5.8–7.5s / max 14.3s |
| 微基准（Windows，Python 3.14） | 集合比较 / 并集 / JSON 编码的绝对成本（VPS 上约 ×2–3） |
| 前端可达性追踪 + 现有 `web/dist` 产物 | 死接口判定、包体积、资源尺寸 |

**当前生产基线**（162 拍样本）：tick 间隔 mean 16.1s，单拍耗时 mean 1,057ms / p50 982ms / p90 1,180ms / max 3,974ms；其中 `submit`（网络）739ms = 70%，`map_memory` 161ms = 15%，`unit:worker` 88ms = 8%，`resource_assign` 29ms。3.1% 的拍次 >1.5s。

---

# P0 · 会直接造成损失的问题

## 1. 仪表盘把「环回地址」当成「已登录」→ CSRF + WebSocket 劫持游戏 API Key

**证据** `[代码]`

```python
# dashboard.py:2860-2862
def _is_loopback(self) -> bool:
    host = self.client_address[0] if self.client_address else ""
    return host in ("127.0.0.1", "::1", "localhost")

# dashboard.py:2890-2891
def _authed(self) -> bool:
    return self._is_loopback() or self._token_ok(self._client_token())
```

- 全文件 **没有任何 `Origin` / `Referer` 校验**（3,861 行，grep 零命中）。
- `_parse_body`（`dashboard.py:2933-2938`）**忽略 `Content-Type`**，任何 `text/plain` 的跨站 POST 都能被解析成 JSON —— 这是 CORS 的「简单请求」，浏览器直接放行。
- WebSocket 分支 `_proxy_game_ws`（`dashboard.py:3184-3238`）由 `do_GET` 直接喂入，上游请求头带的是服务端密钥：`dashboard.py:3254` `f"Authorization: Bearer {ARENA_API_KEY}"`。**浏览器对 WebSocket 不做 CORS 预检**。

**影响**：README 文档推荐的使用方式是 `http://localhost:4399`（`README.md:144-145`），此时**每一个浏览器请求都是环回**，token 变成纯装饰。用户访问的任意一个网页都可以：改写机器人策略配置、提交游戏指令、并通过 WS 拿到一条带真实 API Key 的游戏数据管道。局域网/容器内任意进程同理。

**修法**
1. 所有改变状态的路径 + WS 升级校验 `Origin` 的 host 必须等于 `Host`，否则 403。
2. 环回旁路只保留给 `GET /api/health`，其余一律要 token。
3. `_parse_body` 强制 `Content-Type: application/json`（不匹配 → 415）。
4. WS 改成先用已鉴权 GET 换一次性 ticket 再连接。

---

## 2. 已「下线」的服务端渲染面板仍然在线：单次请求 20.8 MB / 约 2s CPU / 峰值 240 MB

**证据** `[代码]` `[实测]` `[日志]`

- SPA **从不调用** `/api/state?full=1`，也不调 `/api/log`（对 `web/src` 全量 grep：前端只消费 `/api/battle-log|config|teams|holds|waypoints|squad-targets|unit-routes|enemy-memory|unit-names|guerrilla-tracks|v1/*`）。
- 但 `do_GET` 仍然保留这条路径：`dashboard.py:3409` `parts = build_parts(...)` → `dashboard.py:3413` `json.dumps(parts)`。
- `build_parts`（`dashboard.py:2327`，456 行）→ `_render_svg_cached`（1110）→ `render_svg`（1910，415 行），把**全部记忆墙体画进一张 SVG**。
- 按生产规模（45 万墙）实测：`load_map_memory` 冷 0.73s、`render_svg` 1.16s → 18.5MB SVG、`json.dumps` 0.12s → **20.8MB body**、gzip +0.17s，**峰值分配 238MB / 常驻 95MB**（`_svg_cache`）。`_QuietHTTPServer` 无并发上限、`_svg_cache` 无 single-flight，N 个并发即 N 倍。
- 项目自己的巡检日志已经记录过后果：`INSPECTION_LOG.md:12` —— `/api/state` 响应体 **22,100,454 字节**、冷 **4.97s**，而 compose healthcheck 是 `urlopen(timeout=3)`，导致容器 `Up 2 days (unhealthy)`。

**影响**：容器里唯一的 HTTP 进程即可被一次 `curl` 拖死（SPA 与游戏代理同时不可用）。

**修法**：删掉 `full=1` 分支连同 `build_parts` / `render_svg` / 其依赖（见 #11）；`/api/state` 退化为 `{ok,tick}` 或直接删除（`/api/health` 已覆盖探针）。

---

## 3. 每次部署上传 112.8 MB 垃圾；Docker 构建上下文 115.1 MB

**证据** `[代码]` `[实测]`

```python
# deploy/deploy.py:110   ← 唯一的临时文件规则
r"^_.*\.py$",
```

```text
# .dockerignore:27      ← 同一条规则
_*.py
```

仓库根目录有 **55 个 `_*` 临时件、合计 112.8 MB**（`_remote_state_latest.json` 21.9MB、`_remote_tacticlog_now.jsonl` 13.4MB、`_remote_map_memory_current.json` 10.5MB、`_remote_routes_snapshot.jsonl` 8.4MB、4 个 `_remote_guerrilla_routes-*.jsonl` 各约 5MB …）。`_*.py` 规则拦不住 `_*.json` / `_*.jsonl` / `_*.txt`。

- `transfer_files()`（`deploy/deploy.py:156-195`）整树 `sftp.put`，无体积/哈希/增量判断 → 每次部署推送 **112.8 MB**。
- 用真实 `.dockerignore` 语义复算：**构建上下文 115.1 MB / 231 文件，其中 112.0 MB 是那 36 个 `_*` 文件**（真正的源码约 3 MB）。

**修法**：两个文件都改成 `_*`（或 `_remote_*`）+ 同一条 glob；把 55 个临时件移出仓库（对象存储/`archive/`）；`deploy.py` 改成 `deploy_source_only.py:44-55` 那种显式清单上传。

---

## 4. 部署链路：`AutoAddPolicy` + root 密码 + 把 token 打进控制台命令后原样打印

**证据** `[代码]`

```python
# deploy/deploy.py:302-303
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())   # 接受任意主机密钥
client.connect(HOST, PORT, USERNAME, PASSWORD, timeout=15)     # DEPLOY_USER 默认 "root" (:56)

# deploy/deploy.py:339-354
cmd = f"curl ... -H 'Authorization: Bearer {DASHBOARD_TOKEN}' ..."
print(f"  $ {cmd}")        # token 原样进终端 scrollback / CI 日志 / shell history
```

同款代码在 `deploy/deploy_source_only.py:59-60, 125, 139`。这条通道上传的是 51 字符的**线上游戏 API Key** 与 `DASHBOARD_TOKEN`；任何 deploy 路径上的 MITM（`AutoAddPolicy` 不校验）都能拿到。

**修法**：`load_system_host_keys()` + `RejectPolicy`；改密钥登录 + 非 root 受限用户；打印前 `_mask(token)`；轮换已暴露的两个凭据。另外 `.env` 与 `deploy/.env.deploy` 目前用的是**同一个 API Key**，没有轮换边界。

---

## 5. 健康检查与进程守护都检测不到「机器人其实没在跑」

**证据** `[代码]` `[日志]`

- `/api/health`（`dashboard.py:3384-3394`）只看日志里最后一个 tick 号，**不判新鲜度**，也不问 tactic 进程死活；`compose.yml:22-33` / `Dockerfile:52-53` 只探它。
- `docker-entrypoint.py:65-71`：没有 `ARENA_HERO_API_KEY` 时 `start_tactic()` 返回 `None`，`while True`（`:131`）于是只盯 dashboard → **容器长期 healthy，但没有机器人在玩**。
- `docker-entrypoint.py:164-171`：tactic 崩溃 → `sleep(2)` → 重启，**无计数、无退避、无最大重试、不退出**。启动即崩的死循环永远不会触发 `restart: unless-stopped`。
- `tactic_play.log` 在 `docker-entrypoint.py:42,74` 以 append 打开，**永不轮转**，而它是 60 处 `print()` + 全部 `stall_alert` 的落点。`compose.yml` 也没有 `logging.max-size`。

**修法**：`/api/health` 返回 `tick_age_s` + tactic 心跳文件，超过阈值直接 503；entrypoint 加指数退避 + 失败计数，超 N 次非零退出让 Docker 重启；`tactic_play.log` 沿用 20MB/3 份策略，compose 加 `logging` 上限。

---

# P1 · 每 Tick 的确定性浪费（有实测数字）

## 6. 每拍对 48.8 万格墙体做一次全量集合比较

**证据** `[代码]` `[实测]`

```python
# tactic.py:8390-8391
before = len(_obstacle_memory)
if len(_known_obstacles) != before or _known_obstacles != _obstacle_memory:
    #   ^ 长度相等时短路失败，落到第二个条件 → 全量比较 48.8 万格
```

长度不变是常态，因此**每一拍**都会执行 `frozenset(48.8万) != set(48.8万)`。

- 本机实测：742k 格 **32.0 ms/次**；按 48.8 万折算约 **21 ms/拍**，VPS 上约 ×2–3。
- 该语句位于 `map_memory` 相位（`tactic.py:9097-9107`），生产该相位 **p50 144ms / mean 161ms**，占 tick 总耗时 15%。

**修法**：墙体只增不减（全文件只有 `.add()`），把相等判断换成**单调版本号**：`_obstacle_mem_generation += 1`（每次 add）+ `_known_gen`，比较两个整数。测试要改标记时显式 `bump_generation()`。

---

## 7. 每次 A* 调用重建 48.8 万～81 万格的 `blocked` frozenset

**证据** `[代码]` `[实测]`

```python
# tactic.py:806-813
if not extras and (obstacles is _path_blockers_union_key
                   or obstacles == _path_blockers_union_key):
    union = _path_blockers_union
else:
    union = obstacles | extras | dead      # 全量并集，且 extras 非空时永不缓存
    if not extras:                          # ← 所以带 extras 的调用永远是冷的
        _path_blockers_union_key = obstacles
        _path_blockers_union = union
```

```python
# tactic.py:876-877   ← 第二份全量拷贝
if goal in blocked and goal not in obstacles and goal not in extras:
    blocked = blocked - {goal}
```

`extras` 在生产里几乎总非空：`_plan_worker` 传 `extras=extras_here`（`tactic.py:2600-2606`）、`_move_towards` 传 `extras=transient_obstacles`（`3818-3824`）、`_kite_bfs_unstick`（`5389-5394`）。

- 本机实测：`obstacles | 3格 | dead`（81.0 万格）**72 ms/次**；`blocked - {goal}` **27 ms/次**。
- 与生产遥测完全吻合：`pathfind` 每次调用**中位数 64 ms，却只展开 176 个节点** —— 每个节点的分摊成本是 A* 本身的 100 倍。
- 16～19 次调用的那几拍，`pathfind_ms` 813–838ms、`unit:worker` 1,425–1,443ms。

**修法**：不要物化并集。A* 内部改判 `cell in obstacles or cell in dead or cell in extras`（3 次哈希 ≪ 72ms）；`goal` 用邻居判定里的 override 处理，取消 `-{goal}` 拷贝。退一步：按 `(id(obstacles), id(dead), frozenset(extras))` 记忆化（`extras` 每个调用方都 ≤30 格）。

---

## 8. 战斗单位拥堵时复制整张墙集 → 触发 4.2 秒的全量死胡同重算（命令窗炸弹）

**证据** `[代码]` `[实测]`

```python
# tactic.py:10221-10224（先锋，游侠 10250-10253 同款）
_extra_block = {c for c in (_blocked_cell, _contest_cell) if c is not None}
_unit_obstacles = (
    obstacle_cells | _extra_block if _extra_block else obstacle_cells
)
```

- 这一步本身：48.8 万格并集 ≈ **78 ms/单位**（本机），k 个受阻单位 = k 次。
- 更严重的是**它破坏了缓存身份**：`_unit_obstacles is not _dead_obstacles`，于是 `_get_dead_ends` 掉进 `tactic.py:655-656` 的批处理分支 → `_dead_end_cells_batch`（`478-514`）。按生产地图实测：**4,176 ms、2,133,659 个候选格、48,733 个死胡同**，且瞬时分配两个 213 万条的容器（2GB VPS 上有 OOM/swap 风险）。
- 触发条件是常规的：非工人连续 3 次 `CELL_UNIT_LIMIT`（`7187-7196`）或 `MOVE_CONTESTED` 驻留（`7199-7213`）—— 正是这个 bot 长期在打的「核心环拥堵」。
- `PLAN_PATHFIND_BUDGET_S = 2.0` 的 deadline 只在 `_bfs_path` 的循环里检查（`889`），**在 `_path_blockers` 之前就已超支**。

**修法**：永远不要把新墙集交给规划器 —— 把这两个格子放进既有的 `extras` / `extra_obstacles` 通道（`_plan_home_combat` 已经在用 `_chute_ring` 这么做，见 `4918, 4944`）。保底：`_dead_end_cells_batch` 开头加 `if len(obstacles) > 100_000: raise/log` 熔断。

---

## 9. fork 后台存盘后，父进程不知道签名已变 → 下一拍重新解析自己写的多 MB 文件

**证据** `[代码]` `[实测]`

```python
# tactic.py:7898-7904（子进程）
if pid == 0:
    try:
        _save_map_memory(tick=tick, force=True)   # 子进程里更新 _map_file_sig_cache
    ...
    os._exit(code)                                 # ← 子进程内存丢弃，父进程一无所知
```

`_map_file_sig_cache` 只在两处被写：`_apply_dashboard_map_edits`（`7623-7630`）和 `_save_map_memory`（`7821`）。**`_reap_map_save_child`（`7839-7878`）成功后不刷新它**。

后果：父进程下一次 `_apply_dashboard_map_edits()` 发现 mtime/size 变了 → `json.loads(MAP_MEMORY_PATH.read_text())`（`7626`）把一整份 6.6–11 MB 文件重新解析。

- 生产遥测正好抓到一个：`tick=589187` **`map_memory = 2,124 ms`**（该拍总耗时 2,939ms），其余拍次该相位稳定在 140ms 左右。
- 每次成功 fork 存盘后必然复现一次，即每 `map_save_interval_ticks`（默认 10）个脏拍一次 2 秒级卡顿。

**修法**：`_reap_map_save_child` 确认子进程 exit 0 后 `_map_file_sig_cache[str(MAP_MEMORY_PATH)] = _file_signature(MAP_MEMORY_PATH)`；或让子进程通过管道回传 `(mtime_ns, size)`。

---

## 10. 每拍 tick 日志 53 KB，其中 87% 是工人 A* 路径坐标

**证据** `[代码]` `[实测]`

```python
# tactic.py:1187-1188
"target": list(route["target"]) if route.get("target") else None,
"path": [list(position) for position in route.get("path", [])],
...
# tactic.py:1240-1241
self._file.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")
self._file.flush()
```

- 实测 162 条真实记录：**mean 53.0 KB / p90 60.4 KB**，其中 `workers` 占 **86.9%**（45.5 KB）。单条实例：25 个工人、23～228 格缓存路径 → 56,755 字节里 46,125 是路径坐标。
- 每拍额外成本：`asdict()` ≈3.0ms + `json.dumps` 0.67ms + 3,075 次 `list()` 分配 0.42ms + `flush()`，**且这段完全在 `_PhaseTimer` 之外**（`play()` 在 `10434-10446` 调用），所以 `phase_ms` 里看不见。
- 20 MB 轮转（`tactic.py:76`）因此**每约 380 拍**就触发一次（`78-82` 的注释仍按 22 KB/拍 ≈890 拍估算，已过时）；而 dashboard 的趋势/战斗日志都靠读这份文件。

**修法**：只记 `path_len` + `path[:2]` + `path_complete`（路线可由服务端重放，前端只是画线），日志直接瘦身约 85%，20MB 预算可容纳约 2,600 拍；`flush()` 改成每 N 拍或复用 OS 缓冲；`_maybe_rotate` 用拍计数代替每拍 `fstat`。

---

# P2 · 结构与工程债（决定后面每一次改动的成本）

## 11. `dashboard.py` 33%（约 1,270 行）是没人调用的 SSR 遗留，还被 29 个测试/651 行锁死

**证据** `[代码]`

| 符号 | 定义 | 唯一调用者 | 真实消费者 |
|---|---|---|---|
| `build_parts`（456 行） | 2327 | `/api/state?full=1`（3409） | **无** |
| `render_svg`（415 行） | 1910 | `_render_svg_cached`（1110） | 同上 |
| `render_waypoints_panel`、`_core_rail_html`、`_seg_cells` | 1697 / 1791 / 1778 | `build_parts` | 同上 |
| `_hotspots_html`、`_combat_hotspots`、`_recent_combat_cells` | 716 / 658 / 617 | `build_parts` | 同上 |
| `_battle_log_html` + `_log_coord_spans` | 554 / 591 | `build_parts` + `/api/log` | **无**（SPA 用 JSON `/api/battle-log`） |
| 孤儿路由 `/api/stream`、`/api/trends` | 3381 / 3441 | — | 无（`web/src` 里没有 `EventSource`） |
| 完全无引用：`read_latest`、`LOG_CATEGORIES`、`TEAM_BOARD_KEYS/META` | 131 / 484 / 1543 | — | — |

`LOGIN_HTML`（2785）**是活的**，非环回 401 时返回它，不要删。

项目自己在 `INSPECTION_LOG.md:17` 记录了这次「只下线 UI 不下线 API」的妥协，`:21` 还明确写了「遗留可选项：`/api/log`、`/api/battle-log`、`/api/stream` 返回的仍是旧面板风格的 HTML 片段，arena 前端并不消费它们」。

**代价**：`tests/test_regressions.py` 里有 **29 个测试函数 / 651 行**在守护这些 HTML/DOM 契约（如 `test_full_state_is_still_available_on_request`，`tests/test_regressions.py:8338`）—— 换句话说，**测试正在为 20MB 的 `/api/state?full=1` 行为背书**，这就是 #2 的回归被冻结下来的原因。

**修法**：删除上表全部符号 + `/api/log`、`/api/stream`、`/api/trends` + 对应的 29 个测试。净减约 1,900 行源码 + 650 行测试，同时消掉 #2。

---

## 12. `tactic.py` 单体：10,587 行、`_choose_actions` 1,268 行、185 个模块级全局

**证据** `[代码]`

- `_choose_actions` 9022→10290，**1,268 行**；`_plan_worker` 668 行；`_plan_kite_combat` 690 行；`_plan_home_combat` 283、`_plan_attack_combat` 251、`_plan_ranger` 244 行。
- **185 个模块级 `_xxx` 全局**、41 处 `global` 语句。规划过程中就改状态（`_delivery_lease_uid`、`_chute_in_demand`、`_lease_holder_adjacent`、`_march_budget_spent`），所以才需要 `_finalize_kite_collision_state`（`5341-5349`）在提交被拒后回滚 —— 而 `turn.submit()` 的结果要到 `play():10365` 才知道。
- 同一段「反振荡尾部更新」手抄 11 次（`recent.append(pos)`；`_worker_recent.get(uid, [])` 出现 14 次）；「已发出退出」的三行样板（`_set_worker_route(...)` + `return (action, detail)`）在函数里重复数十次。
- 这直接造成了 #8：#6 那类修法只在 `_plan_home_combat:4939-4944` 做对了一处（还带了解释性注释），另外两处（`10222`、`10253`）仍在犯同样的错。
- `_object_names`（`957-966`）为**每个见过的敌人 id** 永久保留一条；`_prune_dead_unit_bookkeeping`（`8908-8911`）只清 `W/V/R` 前缀，`C`/`E` 是**故意不删**的（`8886-8887`：敌人可见性间歇，删了名字会漂移）—— 这是个合理取舍，代价是敌人 UUID 频繁更替时该表随运行时长无界增长，且每拍 `list(_object_names.items())` 要全量展开一次。加一条「按 last_seen_tick 的 LRU 上限」即可两全。

**修法（增量，不必一次重写）**
1. 建 `PlanInputs` dataclass，每拍构造一次：`obstacles` / `dead` / `extras` / `cell_counts` / 派生缓存。这一步同时消掉 #7、#8。
2. 把「40 张平行字典」合并成一个 `dict[uid, UnitBookkeeping]`，一次 prune。
3. 抽出 `finish(action, detail)` 收尾助手，替代数十处样板。
4. 把 `_plan_*` 逐个搬到 `planners/` 包（纯函数 + `PlanInputs`），`tactic.py` 只留循环与 I/O。

---

## 13. 4 份 `_data_dir()` 实现，且回落语义不一致 → 换目录启动就写出两套状态

**证据** `[代码]`

| 文件 | 未设 `ARENA_DATA_DIR` 时的回落 |
|---|---|
| `tactic.py:43-46` | `Path.cwd()` |
| `tactic_config.py:14-16` | `Path(__file__).resolve().parent` |
| `game_stats.py:27-29` | `Path(__file__).resolve().parent` |
| `dashboard.py:41-43` | `""`（相对当前目录） |
| `status.py:15-17` | `""`（相对当前目录） |

从别的目录 `python C:\...\tactic.py` 启动，`tactic_log.jsonl` / `map_memory.json` 落在 cwd，而 `tactic_config.json` / `game_stats.json` 落在脚本目录 —— 配置热加载与统计会静默读到不同目录。

**修法**：只在 `state_io.py` 留一份 `data_dir()` / `data_path()`，四处改为 import；顺手把 `LOG_MAX_BYTES`、`_SAVE_INTERVAL_TICKS` 等散落的运行时常量一起收进去。

---

## 14. 零 CI · 零 pyproject · 零 linter · 依赖不锁版本 —— 而 `web/` 侧工具链是完整的

**证据** `[实测]`

- 无 `.github/`（唯一的 workflow 在 gitignore 掉的 `.pi/` 里）、无 `pyproject.toml` / `pytest.ini` / `tox.ini` / `setup.cfg` / `ruff.toml` / `mypy.ini` / `.pre-commit-config.yaml` / `.python-version` / lock 文件。
- `requirements.txt` 只有一行 `arena-hero>=0.2.9,<0.3`；文档要求手动 `pip install paramiko`（`deploy/DEPLOYMENT.md:57`），`pytest` 也没声明。
- 本地开发解释器 **Python 3.14.5**，镜像 `python:3.12-slim`（`Dockerfile:9`），两边都没声明。
- 对比同仓库的 `web/`：`package-lock.json`（157 KB，全钉死）、`eslint.config.js`、三份 `tsconfig`、`vitest` + `@testing-library`、约 50 个就近测试。
- 测试现状：`tests/test_regressions.py` **12,967 行 / 610 个用例 / 70 个类**（README.md:358 仍写「约 165 个用例」，差 4 倍）；无 `conftest.py`，只有 10 处手工 patch `ARENA_DATA_DIR`，仓库根那几个运行时残留文件（`tactic_log.jsonl`、`game_stats.json`、`holds.json.lock`）就是没有隔离的产物。

**修法**：`pyproject.toml`（`[project]` + `[tool.ruff]` + `[tool.pytest.ini_options]`）、`requirements-dev.txt`、一个 GitHub Actions（`ruff check` → `mypy` → `pytest -q`）、`conftest.py` 里 autouse 的 `tmp_path` 版 `ARENA_DATA_DIR`、`coverage --fail-under`。

---

## 15. 仓库塞了 55 个 `_*` 临时件（112.8 MB）与 3 份 97% 相同的 `tactic.py` 快照

**证据** `[实测]`

| 文件 | 行数 | 顶层函数 | 相比 `tactic.py` 缺 |
|---|---|---|---|
| `tactic.py` | 10,587 | 186 | — |
| `_remote_tactic_latest.py` | 8,714 | 153 | **33 个**，含 `_choose_actions`、`_fork_map_save`、`_flush_pending_map_memory`、`_auto_target_rank` |
| `_tactic_baseline.py` | 8,666 | 153 | 同上 |

逐行 diff：8,714 行里 **8,464 行与 `tactic.py` 完全相同（97%）** —— 是过期快照，不是可用的分支版本。

**风险**：这正是「grep 到旧快照 → 把旧行为当成当前行为恢复回去」的事故形状。另外工作树 292.7 MB、`.git` 30.5 MB 且有 1,693 个从未 gc 的松散对象；`core.autocrlf=true` 下 `tactic.py` 是 CRLF 而 `docker-entrypoint.py`/`state_io.py` 是 LF，`deploy.py:188` 又是原始字节上传 —— 部署到远端的文件与提交的字节不一致（这也是 `deploy/_rollback_tactic.py:34-37` 存在的原因）。

**修法**：删三份快照 + 52 个临时件（外移存档）；`.gitattributes` 加 `* text=auto eol=lf`；`git gc --prune=now`。

**补充（顺手可删的死代码）**：`direct_wrapper.py`（555 行，被 `.dockerignore:24` 与 `deploy.py:115` 双重排除，镜像里不存在）＋ `watchdog.py:213-339`（127 行 Windows-only 的 `wmic` / `taskkill` / `DETACHED_PROCESS` 守护，容器内必然失败），而 `watchdog.py:27` 还在算一个容器里不存在的 `WRAPPER_SCRIPT` 路径。`watchdog.py` 的**前半段**（`update_stall_tracking`）是活的，必须保留。

---

# P3 · 前端（`web/`）

## 16. 迷雾层每帧扫描整个视口，每格一次模板字符串分配

**证据** `[代码]`

```ts
// WorldCanvas.tsx:317   ← 在 useLayoutEffect 里，依赖数组含 camera（:361）
drawFogAndRouteTints(backgroundContext, size, camera, explored, visible, unitRouteTints)

// WorldCanvas.tsx:700-705
for (let y = minY; y <= maxY; y++) for (let x = minX; x <= maxX; x++) {
  const key = `${x},${y}`          // 每格一次字符串分配
  if (visible.has(key) || !explored.has(key)) continue
  ctx.fillRect(...)
}
```

`camera` 由 `scheduleCamera` 以 `CAMERA_FRAME_INTERVAL_MS = 1000/60`（`:78, :180-197`）提交，即**平移时 60fps 重跑整个 layout effect**。`MIN_WORLD_CELL_SIZE = 3`（`lib/worldCanvasPerformance.ts:10`）下，1600×900 视口 = **533×300 ≈ 16 万次迭代/帧**，每帧一次字符串 + 两次 `Map/Set` 查询 + 最多 16 万次 `fillRect`。而 `explored` 在长期运行后是 10⁵–10⁶ 量级（生产墙记忆已达 45 万～76 万格）。

**修法**：迷雾只随 `visible`/`explored`（每 tick 一次）变化，不随相机变化 —— 按 tick 版本号烘焙进一张世界坐标离屏画布再随相机 blit；或按 chunk 建 `explored` 索引，只遍历「视口 ∩ 已探索」；`cell < 8` 时直接跳过逐格迷雾。

## 17. 每个 tick 8 个独立 HTTP 轮询，无 `AbortController`、无可见性门控

**证据** `[代码]`

`ArenaPage.tsx:59-75` 在每次新 tick 触发 `useUnitNames` / `useUnitTeams` / `useEnemyMemory` / `useWaypoints` / `useSquadTargets` / `useHolds` / `useUnitRoutes`，再加 `BattleLogPanel.tsx:100` 一次拉 **最多 3,000 行**（`lib/battleLog.ts:23-25`）。每个 loader 只有一个 `cancelled` 布尔，**没有一处 AbortController**（全 app 仅 `lib/api.ts:104` 有），也没有 `document.visibilityState` 判断（仅 WS 看门狗 `useGameStream.ts:146` 有）→ 后台标签页照样全量轮询。

叠加服务端成本：`/api/enemy-memory`（`dashboard.py:3474`）会为返回 63 条目击而 **`decode_obstacles` + `sorted(45 万格)`（实测冷 0.73s / 65MB 峰值）**，且 tactic 每拍都在重写 `map_memory.json` 让缓存恰好失效。

**修法**：合并成一个 `/api/tick` 聚合端（或走 WS 推送增量）；`tick` 变化时 abort 上一个信号；`document.hidden` 时停轮询；`/api/enemy-memory` 给 `enemy_sightings` 单独成段/单独文件，别为它解 45 万格墙体。

## 18. 同一个 tick 里 A* 算两遍，且各自重建一次导航上下文

**证据** `[代码]`

```ts
// ArenaPage.tsx:210（渲染用，为了画路线）
const movementRoutes = useMemo(() => buildMovementRoutes(game.state, game.explored, movementGoals, effective.plan), [...])
// ArenaPage.tsx:196（副作用用，为了发指令）
const result = applyAutonomousMovement(game.state, game.explored, movementGoalsRef.current, currentPlan)
```

两次都会新建 `createNavigationContext`（`lib/pathfinding.ts:199-217`，遍历全部 `state.objects` 及每个 `OBSTACLE/RESOURCE` 的 `positions` 数组），然后每个目标跑一次 `findMovementPathInContext`，各自上限 `MAX_SEARCHED_CELLS = 50_000`。`applyAutonomousMovement` 还每 tick 两次 `JSON.stringify` 整个 plan 只为判等（`pathfinding.ts:147, 170`）。

**修法**：上下文按 `state.objects` 身份记忆化，一次 tick 建一次；让 `applyAutonomousMovement` 把它算好的路线返回给 `ArenaPage`；plan 用结构化比较替代 `JSON.stringify`。

## 19. 约 900 KB 未压缩 PNG 精灵在 `/arena` 挂载时就全部下载解码

**证据** `[实测]`

`WorldCanvas.tsx:251-285` 无条件 `new Image()` 9 张：`beacon.png` **255.1 KB @512×512**、`worker.png` 114.9 KB、`core.png` 113.0 KB、`vanguard.png` 98.1 KB、`asteroid-large-{1,2}` 137.2 KB、`crystal-{1,2}` 117.8 KB、`ranger.png` 63.4 KB，合计 **899.5 KB**；`index.html:8` 还给所有路由 preload 了 `background.webp`（37.4 KB），而 `/login`、`/leaderboard` 根本不画地图。

实际绘制尺寸远小于贴图：beacon `ceil(cell*1.5)`（`WorldCanvas.tsx:1081`）= 最大 **117 CSS px**，单位精灵 `cell*0.62` ≈ 48 px（`lib/unitArt.ts:19`）。512² 的 PNG 画成 ≤234 设备像素，线性超标 4–5 倍。

**修法**：按 ≤2× 最大绘制尺寸重导出 WebP（beacon 256²、单位 96–128 px 高），整套可压到约 120–180 KB；按 `unit_type` 懒加载；障碍/矿石图层关闭时不下载；`background.webp` 的 preload 从 `index.html` 挪到 arena 路由。

---

# 快速见效清单（按 收益/成本 排序）

| # | 动作 | 成本 | 直接收益 |
|---|---|---|---|
| 1 | `.dockerignore` + `deploy.py:110` 改成 `_*` | 2 行 | 每次部署 −112.8 MB，构建上下文 115→3 MB |
| 2 | 删 `/api/state?full=1` 分支 | 1 段 + 29 测试 | 消除 20 MB / 240 MB 峰值 / 容器 unhealthy 根因 |
| 3 | `_reap_map_save_child` 刷新 `_map_file_sig_cache` | 2 行 | 消除每次存盘后约 2s 的卡顿拍 |
| 4 | `_obstacle_memory` 比较改成版本号 | ~10 行 | 每拍 −21～32 ms（`map_memory` 相位的最大单项） |
| 5 | `10221-10224` / `10250-10253` 改走 `extras` | ~6 行 | 消除 78 ms/单位 与潜在的 4.2s 命令窗炸弹 |
| 6 | WS / 状态变更路径加 `Origin` 校验 | ~30 行 | 关掉 CSRF + WS API-Key 劫持 |
| 7 | tick 日志只记 `path_len` + 首尾 | ~10 行 | 日志 −85%，每拍 −4～5 ms，轮转频率 ×7 |
| 8 | `_data_dir` 收敛成一份 | ~20 行 | 消掉一个静默的数据分裂 bug |
| 9 | entrypoint 加退避 + 失败计数 + `/api/health` 判 tick 新鲜度 | ~40 行 | 从「healthy 但没在跑」变成可告警 |
| 10 | 删 `direct_wrapper.py` + `watchdog.py:213-339` | 删 682 行 | 消掉容器内必然失败的 Windows 分支 |

---

# 已核查「不是问题」的部分（省下重复调研）

| 传言 / 直觉 | 核查结论 |
|---|---|
| `deploy/.env.deploy`、`.env` 被提交 | **否**。`git ls-files` 只有两份 `.example`；两文件均被 `.gitignore` 忽略。 |
| 密钥泄漏进 git 历史或 112 MB 转储 | **否**。四个线上值与全部文本文件、`git grep -l --all` 比对，只出现在 `.env` 与 `deploy/.env.deploy`。（但两者是**同一个** API Key，没有轮换边界。） |
| 容器以 root 运行 / 无 healthcheck / 无重启策略 | **否**。`USER arena` uid 10001、`HEALTHCHECK`、`init: true`、`stop_grace_period: 20s`、`restart: unless-stopped` 都在；问题在于 healthcheck 的**内容**（#5），不是缺失。 |
| `render_svg` 是不可达死代码 | **半对**。它不是不可达 —— 经 `/api/state?full=1` 可达；但**没有任何客户端调用它**，所以按「实际使用」判死，删法与 #11 一致。 |
| 测试打真实网络 / 启动真实 `tactic.py` 进程 | **否**。用 `ThreadingHTTPServer(("127.0.0.1", 0))` 与 monkeypatch 的 `urlopen`；`tactic` import 无副作用（副作用都在 `if __name__ == "__main__"` 之后）。 |
| 地图是「几千个 SVG 节点」所以卡 | **否**。`WorldCanvas` 是双 canvas + 8×8 chunk 贴图缓存；真问题是迷雾层（#16）与双份 A*（#18）。 |
| 前端缺代码分割 / `lucide-react` 没 tree-shake | **否**。所有路由 `App.tsx:6-11` 都 lazy；40 个具名图标导入，`dist` 里没有 lucide 整包 chunk。 |
| 测试用例数约 165（README.md:358） | **否**。实际 610 个用例 / 12,967 行；README 差 4 倍，需订正。 |

---

# 建议的实施顺序

1. **本周（止血）**：快速清单 1–3、6 —— 全是小改动，分别对应「部署慢」「容器 unhealthy」「每拍 2s 卡顿」「可被跨站控制」。
2. **下一轮（热路径）**：4、5、7 —— 都在 `tactic.py` 的 `_choose_actions` 与 `_path_blockers` 附近，改动可被现成的 `phase_ms`/`pathfind_ms` 逐拍验证（改完看 `map_memory` 相位与 `pathfind` 每节点成本是否下降）。
3. **然后（减债）**：11 → 12 → 13 → 14。顺序很重要：先删掉 1,270 行死 SSR，`dashboard.py` 的可读性才够支撑后续把鉴权/守护重写（#5）。
4. **最后（前端）**：16、17、18、19 —— 只有在操作台真的开始卡、或想支持多标签同时开时才紧急；当前单标签场景下 8 个轮询在 16s 的 tick 周期里还撑得住。
