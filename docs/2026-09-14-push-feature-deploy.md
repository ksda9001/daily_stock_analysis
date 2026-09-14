# 行情推送特性 · 部署手册

> 特性：每天两次从 CowAgent 向用户推送「大盘 + 自选股」行情，用户可取消或改时间。
> 设计日期 2026-09-14。**本文件是部署的唯一依据**，执行前先读「回滚」一节。
>
> ## ✅ 部署状态：已于 2026-09-14 18:32 执行完成
>
> | 步骤 | 状态 | 结果 |
> |---|---|---|
> | A. DSA 侧 push 端点 | ✅ 已上线 | 端点存在；`skip`/`force`/`401` 三种语义实测通过 |
> | B1. 改构建源 | ✅ | mcp_server 与仓库 md5 一致；Dockerfile:19、compose:45 已加 |
> | B2. 重建镜像 | ✅ | `301ba68bdfa2`（旧 `f1870f49a074` 留作回滚） |
> | B3. 重建容器 | ✅ | 6 条 override 挂载齐备；`integration.py` md5 = `0f2d4b7a…` |
> | C. 端到端 | ✅ | 30 工具在册含 `dsa_push_digest`；任务自动补建实测成功 |
>
> **实战发现（写进 §4 了）**：本次部署额外踩到两点 —— ① 容器重建用的是
> `docker compose up -d`（compose 管理，非 `docker run`）；② 重建前先
> `docker rename` 留档，比原方案多一个当次回滚点。

## 0. 设计要点（为什么这样做）

| 问题 | 决定 | 理由 |
|---|---|---|
| 推送时刻 | **09:35 + 15:30** | 见 §0.1 |
| 谁判定「该推了」 | **CowAgent 每 15 分钟轮询 DSA** | DSA 是唯一真相源，取消/改时间只改一处，零跨服务同步 |
| 内容形态 | 纯文本 Markdown | 微信/企业微信都能直接渲染，无附件依赖 |
| 默认开启 | 用户**第一次跟 cow 说话**时自动建任务 | `RecipientStore` 只在收到入站消息时记录收件人 → 天然锚点 |
| 去重 | DSA 侧 `__PUSH_LAST_SENT_<HHMM>` 内部键 | 轮询必然重复问，幂等必须在服务端 |
| 不打扰 | 未到点返回 `{"skip": true}`，CowAgent 侧**抑制投递** | 否则每 15 分钟发一条「还没到点」 |

### 0.1 为什么是 09:35 和 15:30

- **09:35**：集合竞价（09:15-09:25）已定开盘价，连续竞价 09:30 开始；头 5 分钟的
  瞬时跳价已消化，方向基本确立，用户还有整个上午可操作。
- **15:30**：15:00 的收盘价 / 成交额 / 涨跌家数已落定，且创业板、科创板的
  盘后固定价格交易窗口（15:00-15:30）刚好关闭，数据不再变动。

⚠️ **关键前提：推送与分析是两件事。** 09:35 那次**不需要**跑完整分析
（个股约 105 秒/只），只取指数 + 自选股实时行情（单次约 2-4 秒，3 只约 10 秒）。
完整分析已在 08:45 / 18:00 跑。**这是 09:35 可行的唯一原因。**

## 1. 已完成（无需重做）

- **DSA 侧代码**：commit `48f92c1`，已推送；服务器仓库已 `reset --hard` 到该提交
  - 新增 `src/tenancy/push.py`（16665 B）、`tests/test_tenancy_push.py`（34 例）
  - 改 `src/scheduler.py`（`DEFAULT_PUSH_TIMES`）、`src/config.py`、`src/tenancy/settings.py`、
    `src/tenancy/api.py`（`/push/digest`）、`mcp_server/{tools,server}.py`（工具数 29→30）、
    `static/dsa-wechat.js`（设置页推送卡片）
  - 测试：`184 passed / 3 skipped`（tenancy + mcp + scheduler 全套）
- **CowAgent 侧 override**：`integration.py` 已构建并上传
  - 路径 `/root/cow-build/overrides/integration.py`，md5 `0f2d4b7ac2db4bc353a9c244a378de44`
  - 对照原版 `integration.py.orig`，md5 `604b7a944655b5c04a71aae6231be49b`
    （**与容器现役版本逐字节相同** → 补丁是干净的增量）
  - 三处插入：`_should_suppress_delivery`（空结果不投递）、
    `_ensure_push_task`（新会话自动补建任务）、`_PUSH_TOOL_NAME = "dsa_push_digest"`
  - `py_compile` 通过，纯 LF（0 个 CRLF）

## 2. 待执行

### A. DSA 侧 —— 让 `/api/v1/tenancy/push/digest` 上线

```bash
# A1 备份当前镜像（回滚用）
docker tag dsa-fork:deploy dsa-fork:pre-push-20260914

# A2 确认 app.env 与容器内 /app/.env 一致
#    当前两边 md5 均为 0ad12598ac058c773d97518be34eb45c（已核对，无漂移）
md5sum /root/dsa-fork/app.env
docker exec dsa-server md5sum /app/.env
# 若不一致：先 docker cp dsa-server:/app/.env /root/dsa-fork/app.env 再继续

# A3 构建
cd /root/dsa-fork && docker build -f Dockerfile.patch -t dsa-fork:deploy . 2>&1 | tail -5

# A4 重建容器（参数已从 docker inspect 实测抄录，2026-09-14）
docker rm -f dsa-server-prev 2>/dev/null
docker rename dsa-server dsa-server-prev
docker run -d --name dsa-server --network dsa-network --restart unless-stopped \
  -p 8000:8000 \
  -v /root/FI/data:/app/data -v /root/FI/logs:/app/logs -v /root/FI/reports:/app/reports \
  dsa-fork:deploy \
  python main.py --serve-only --host 0.0.0.0 --port 8000
# 实测原容器：image=dsa-fork:deploy / network=dsa-network / restart=unless-stopped
#             port=0.0.0.0:8000->8000 / cmd=python main.py --serve-only --host 0.0.0.0 --port 8000
#             entrypoint=/usr/local/bin/docker-entrypoint.sh（镜像自带，不用写）

# A5 验证（判据）
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/api/v1/tenancy/capabilities   # 期望 200
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/api/v1/tenancy/push/digest -X POST \
  -H 'Content-Type: application/json' -d '{}'                                                # 期望 401（无 token）
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/api/v1/tenancy/nope          # 期望 401（对照）
```

### B. CowAgent 侧 —— 让 `dsa_push_digest` 存在 + 抑制 + 自动补建

```bash
# B1 备份镜像与容器
docker tag cowagent-dsa:latest cowagent-dsa:pre-push-20260914

# B2 更新 MCP 源码（构建源）
cp /root/dsa-fork/repo/mcp_server/*.py /root/cow-build/mcp_server/
grep -c push_digest /root/cow-build/mcp_server/tools.py /root/cow-build/mcp_server/server.py  # 期望 1 / 2

# B3 Dockerfile 追加第 6 行 COPY（在 policy.py 那行之后）
#     COPY overrides/integration.py /app/agent/tools/scheduler/integration.py
#     ⚠️ 必须同时 COPY —— 否则一旦不带挂载参数重建容器，补丁会静默消失（§6.3 教训）

# B4 compose 追加第 6 个挂载（在 policy.py 那行之后）
#     - /root/cow-build/overrides/integration.py:/app/agent/tools/scheduler/integration.py:ro
#     ⚠️ compose 改 environment/volumes 必须重建容器，restart 不够

# B5 构建 + 重建
cd /root/cow-build && docker build -t cowagent-dsa:latest . 2>&1 | tail -5
cd /root/cowagent && docker compose up -d

# B6 验证（判据）
docker exec cowagent sh -c 'grep -c push_digest /opt/dsa-mcp/mcp_server/tools.py'   # 期望 1
docker exec cowagent md5sum /app/agent/tools/scheduler/integration.py               # 期望 0f2d4b7a…
docker exec cowagent python3 -c "
import sys; sys.path.insert(0,'/app')
from agent.tools.scheduler import integration as m
print('patch present:', hasattr(m,'_should_suppress_delivery') and hasattr(m,'_ensure_push_task'))"
```

### C. 端到端

```bash
# C1 工具是否在册（期望 46 个工具，且含 dsa_push_digest）
docker exec cowagent python3 -c "
import sys; sys.path.insert(0,'/app')
from agent.tools.tool_manager import ToolManager
tm = ToolManager(); tm.load_tools()
print(sorted(tm.tool_classes.keys()))" | tr ',' '\n' | grep -c push_digest

# C2 直调 DSA 端点（用 cowagent 账号，应返回 skip=not_due 而非报错）
docker exec dsa-server sh -c 'curl -s -X POST http://127.0.0.1:8000/api/v1/tenancy/push/digest \
  -H "Content-Type: application/json" -H "Authorization: Bearer $TOKEN" \
  -d "{\"wechat_id\":\"o9cq807srKtsAWg3EE9kaM3vHYiU@im.wechat\"}"'

# C3 force=true 应产出真实 digest（大盘 + 自选股）
#    → 判据：返回体含 indices / watchlist 两段，且非 skip

# C4 到点投递：把某用户 PUSH_TIMES 临时设成「当前时刻前 1 分钟」，
#    等一轮轮询（≤15 min）或手动触发 scheduler，确认微信收到消息
#    ⚠️ e2e 判据要看文件系统/收到的消息，不要 grep 转录（提示词里含命令文本会假阳性）
```

## 3. 回滚

```bash
# DSA 侧
docker rm -f dsa-server && docker rename dsa-server-prev dsa-server
docker tag dsa-fork:pre-push-20260914 dsa-fork:deploy   # 若要连镜像一起退

# CowAgent 侧
docker rm -f cowagent && docker rename cowagent-prev-20260914b cowagent
docker tag cowagent-dsa:pre-push-20260914 cowagent-dsa:latest
# 或回退构建源：cp /root/cow-build/overrides/integration.py.orig /root/cow-build/overrides/integration.py
```

**本次部署后新增的可用回滚点**（比上面更近）：

| 回滚点 | 内容 |
|---|---|
| 镜像 `cowagent-dsa:pre-push-20260914` | 部署前版本，ID `f1870f49a074` |
| 容器 `cowagent-prev-20260914c` | 部署前容器（rename 留档） |
| `cowagent-prev-20260914b` | 更早一轮的容器 |
| `/root/dsa-fork/tools/_verify_push_c.py` | C2/C3 验证脚本（可重跑） |
| `/root/dsa-fork/tools/_verify_push_e2e.py` | C4 任务补建验证脚本（可重跑） |
| `/tmp/_watch_push_tick.sh` 的产物 `/tmp/push_watch.log` | 到点触发观测日志 |

**注意**：回滚微信侧不需要重扫二维码 —— 状态在两个 bind mount 里，凭据路径不含容器标识。本次重建后实测凭据文件时间戳（14:27）未变，通道自动恢复。

## 4. 风险与注意

- ⚠️ 重建 `dsa-server` 会**短暂停机**（数十秒），`trader01` 会受影响
- ⚠️ 改 `overrides/` 里的文件**必须就地改写**（`cat tmp > target`），不能 `mv`
  —— `-v` 绑的是 inode 不是路径
- ⚠️ `.dockerignore` 与 `Dockerfile` 是一对，构建失败先查它
- ⚠️ 服务器**不能 push**，只能 `git fetch` + `reset --hard`
- 安全遗留：`docker logs cowagent` 每次启动明文打印 `web_password`（与 root SSH 同口令）、
  `mcp.json` 里 `DSA_PASSWORD` 明文 —— 与本次部署无关，但应尽快轮换

### 4.1 本次实战踩到的坑（2026-09-14 18:3x）

1. **`cowagent` 是 compose 管理的，不是 `docker run` 建的。**
   `docker inspect cowagent` 有完整 `com.docker.compose.*` 标签
   （project=cowagent, config_files=/root/cowagent/docker-compose.yml）。
   第 6 轮权限收紧时已迁回 compose。所以 B3 的正确命令是
   `cd /root/cowagent && docker compose up -d`，**不是** `docker run`。
2. **compose 改过 → config-hash 不符 → `up -d` 必然重建容器。**
   实测 `compose=c6f2ff39…` vs `容器=d91d64e4…`，`up -d` 输出
   `Container cowagent Recreate / Recreated / Started`。这是预期行为。
3. **重建前先 `docker rename` 留档。**
   比原方案（依赖 `cowagent-prev-20260914b`）多一个「当次前一版本」容器，
   回滚粒度更细。本次产出 `cowagent-prev-20260914c`。
4. **探针要按真实契约写，别猜字段。**
   本次两处探针 bug：
   - `push/digest` 端点要求 `wechat_id` 或 `tenant_id` 之一，漏了会返回
     `{"ok":false,"error":"missing_target"}` —— 一度误判为端点故障
   - `_should_suppress_delivery(content, action)` 的 `action` 是 **dict**
     （规则在 `action["suppress"]`），不是字符串
   - 任务定义是**嵌套**结构：`action.call_name` / `action.call_params` /
     `action.suppress`，不是扁平字段
5. **`ssh_run.py` 有内部超时**，长 sleep（>10 分钟）会抛 `socket.timeout`。
   需要等待用 `nohup setsid ... &` 放后台 + 轮询产物文件。
6. **租户库是 `/app/data/stock_analysis.db`，不是 `/app/data/dsa.db`。**
   `dsa.db` 存在但是 **0 字节**，`sqlite3` 打开它不会报错，只会说
   `no such table` —— 很容易误判成「表没建」。
   `dsa_users` / `dsa_user_settings` / `dsa_tenancy_audit` 都在
   `stock_analysis.db`（39 张表）里。
   **判断库里有没有表，先按文件大小筛一遍**，别只凭路径猜。
7. **临时改配置用 `ON CONFLICT(tenant_id, key)` 而不是 `UPDATE`。**
   用户可能从未设过该项（本次 3 个租户的 `PUSH*` 行数为 0），
   `UPDATE` 会静默影响 0 行、看起来「改了」其实没改。
   `dsa_user_settings` 的唯一约束是 `(tenant_id, key)`，支持 upsert。

## 5. 实测验证结果（2026-09-14）

### 5.1 判据对照

| 判据 | 期望 | 实测 |
|---|---|---|
| 镜像内 `tools.py` push_digest | 1 | ✅ 1 |
| 镜像内 `server.py` push_digest | 2 | ✅ 2 |
| 镜像内 `integration.py` md5 | `0f2d4b7a…` | ✅ |
| 容器使用的镜像 ID | `301ba68bdfa2` | ✅ |
| 容器内 override 挂载数 | 6 | ✅ |
| 容器内 `integration.py` md5 | `0f2d4b7a…` | ✅ |
| `grep -c push_digest /opt/dsa-mcp/.../tools.py` | 1 | ✅ |
| MCP 工具数 | 30 | ✅ `30 tool(s)` |
| 工具含 `dsa_push_digest` | 是 | ✅ |
| 微信通道存活 | ≥1 | ✅ 1 |
| 原有 5 个 override 未丢 | 不变 | ✅ 全部 md5 一致 |

### 5.2 DSA 端点三态（`_verify_push_c.py`）

```
无 token          → HTTP 401                                    ✅
带 token 未到点   → {"skip": true, "reason": "missed_window",
                     "push_times": ["09:35","15:30"]}            ✅
force=true        → {"skip": false, "slot": "09:35",
                     "indices": [6 个真实指数点位],
                     "watchlist": [], "text": "**A股行情速览**…"} ✅
```

实测指数：上证 3885.33 -0.07% / 深证 13384.57 -0.64% / 创业板 3285.58 -1.10%
/ 科创50 1528.27 -1.62% / 上证50 2861.05 -0.25% / 沪深300 4480.08 -0.67%

### 5.3 抑制逻辑（`_should_suppress_delivery`）

契约：`content` 为字符串，`action` 为 dict（规则在 `action["suppress"]`）。

| 输入 | 期望 | 实测 |
|---|---|---|
| 空字符串 / None / 空白串 | 抑制 True | ✅ |
| `{"skip": true, ...}` | 抑制 True | ✅ |
| `{"skip": false, "indices": [...]}` | 不抑制 False | ✅ |
| 无 skip 字段的 JSON | 不抑制 False | ✅ |
| 非 JSON（工具报错文本） | 不抑制 False | ✅ |
| JSON 数组（非对象） | 不抑制 False | ✅ |
| 无 suppress 规则（空 dict） | 不抑制 False | ✅ |

**接线已验证**：`integration.py:1149` 真实调用，非死代码。
`_ensure_push_task` 同（`:1339`，挂在 `recipient_store.remember()` 之后）。

### 5.4 任务自动补建（`_verify_push_e2e.py`）

模拟一次微信入站消息后，`tasks.json` 实测产出：

```json
{
  "id": "dsa-push-weixin-o9cq...@im.wechat",
  "name": "DSA 行情推送（大盘 + 自选股）",
  "enabled": true,
  "schedule": {"type": "interval", "seconds": 900},
  "action": {
    "type": "tool_call",
    "call_name": "dsa_push_digest",
    "call_params": {"wechat_id": "o9cq...@im.wechat"},
    "receiver": "o9cq...@im.wechat",
    "channel_type": "weixin",
    "suppress": {"json_field": "skip", "equals": true}
  },
  "next_run_at": "2026-09-14T18:58:02"
}
```

日志佐证：`[Scheduler] Provisioned DSA push task dsa-push-weixin-... (channel=weixin, interval=900s)`
**幂等已验证**：重复调用不新增任务（1 → 1）。

### 5.5 回归检查

| 项 | 结果 |
|---|---|
| `dsa-server` / `cowagent` / `searxng` | 全部 Up |
| DSA `/api/v1/tenancy/capabilities` | 200 |
| CowAgent 控制台 `:9899` | 303（登录跳转，正常） |
| 公网 `https://fi.myfi.cc.cd/health` | 200 |
| 错误日志扫描（traceback/critical/ERROR） | 无 |
| 微信凭据文件 | 保留，时间戳未变（**无需重扫二维码**） |
| `weixin_channel.py` 门禁补丁 | 4 处 |
| `web_channel.py` `_resolve_channel_manager` | 9 处 |
| 权限模式 | `read-only` / `SELF_EVOLUTION_ENABLED=False` |

### 5.6 到点轮询实跑（整条链路的决定性证据）

前四节都是**分环节**验证。本节点是把任务放在那儿让它自己跑一次，
观察「调度 → MCP → DSA → 抑制 → 不投递」是否是**同一条链路**真的连通。

任务 `next_run_at=2026-09-14T18:58:02`，到点后实测日志：

```
[18:58:03][scheduler_service.py:114] [Scheduler] Executing task:
    dsa-push-weixin-o9cq...@im.wechat - DSA 行情推送（大盘 + 自选股）
[18:58:03][integration.py:1138] [Scheduler] Task dsa-push-...:
    Executing tool 'dsa_push_digest' with params {'wechat_id': 'o9cq...@im.wechat'}
[18:58:03][mcp_tool.py:27] [McpTool] server=dsa tool=dsa_push_digest params={...}
[18:58:03][mcp_client.py:325] [MCP:dsa] stderr: .../push/digest "HTTP/1.1 200 OK"
[18:58:03][integration.py:1150] [Scheduler] Task dsa-push-...:
    suppressed empty result, not delivered
```

`tasks.json` 随之推进：

```json
"last_run_at": "2026-09-14T18:58:03.079050",
"next_run_at": "2026-09-14T19:13:03.079050"
```

**这一段日志为什么是决定性的**，逐行对应设计意图：

1. `scheduler_service.py:114` —— 间隔调度器真按 `seconds=900` 醒来并派发，
   说明任务不是「写进 JSON 就算完」的死记录。
2. `integration.py:1138` —— 派发走的是 `tool_call` 分支，工具名
   `dsa_push_digest` 与 `_PUSH_TOOL_NAME` 一致，参数带上了 `wechat_id`。
3. `mcp_client.py:325` —— 真的跨进程打到了 DSA 的 `/push/digest`，
   且 **HTTP 200**。这一步证明 MCP stdio 传输 + 内网寻址 + 服务账号鉴权
   三段全通。
4. `integration.py:1150` —— 拿到了 DSA 的 `{"skip": true}`，被
   `_should_suppress_delivery` 判为「空结果」，于是**不投递**。

第 4 行正是「一天里绝大多数轮询都不该发消息」的预期行为。
18:58 不在 `["09:35","15:30"]` 之内，**不投递才是对的**；
如果这里出现了投递，反而是 bug。

**换句话说：本次「没收到微信消息」是预期结果，不是失败。**
真正待确认的是「到点（09:35 / 15:30）时该发出去」，
那需要跨过下一个推送时刻才能观察到（见 §6）。

## 6. 尚未验证的一件事：真到点时是否真收到微信

部署与链路都已验证通过，但**唯一没有实测到的是「到点 → 微信真收到」**。
原因是本次观察窗口（18:58）不在推送时段内，按设计被抑制。

### 6.1 怎么补这一次验证

不需要改代码，也不需要等明天。把推送时间临时挪到「一分钟之后」即可：

```bash
# 1. 看当前时间，设成一个马上要到点的时刻（例：现在 14:20 → 设 14:22）
date +%H:%M

# 2. 改 cowagent 服务账号（dsa_users.id=2）的推送时间
#    ⚠️ 库文件是 /app/data/stock_analysis.db，不是 dsa.db（后者是 0 字节空文件）
#    表 dsa_user_settings 的列是 id / tenant_id / key / value / updated_at，
#    唯一约束在 (tenant_id, key)，所以可以 ON CONFLICT
docker exec dsa-server python -c "
import sqlite3
c = sqlite3.connect('/app/data/stock_analysis.db')
c.execute(\"INSERT INTO dsa_user_settings (tenant_id, key, value, updated_at) \"
          \"VALUES (2, 'PUSH_TIMES', '14:22', datetime('now')) \"
          \"ON CONFLICT(tenant_id, key) DO UPDATE SET value=excluded.value, \"
          \"updated_at=excluded.updated_at\")
c.commit()
print(c.execute(\"SELECT tenant_id,key,value FROM dsa_user_settings \"
                \"WHERE tenant_id=2 AND key LIKE 'PUSH%'\").fetchall())
"

# 3. 等一轮轮询（间隔 900s）。不想等就重置 next_run_at 逼它立刻跑：
#    改 tasks.json 后重启 cowagent 让调度器重载
```

到点后在**微信里直接看**是否收到「A股行情速览」。

### 6.2 判据（三选一，按可信度排序）

| 方式 | 看什么 | 注意 |
|---|---|---|
| **微信客户端** | 真收到消息 | 最直接，唯一无假阳性的判据 |
| `tasks.json` | `last_run_at` 推进 | 只证明跑了，**不证明发出去了** |
| `docker logs` | 有 `suppressed` 就是没发 | 若**没有** `suppressed` 且有投递动作 → 发了 |

⚠️ **不要用 `grep` 转录来判「有没有发」**：提示词与工具描述里本身就含
`dispatch` / `发送` 等字样，grep 会假阳性（§4.1 第 3 条同源教训）。

### 6.3 验完记得改回去

```bash
# 恢复默认推送时间：删掉用户级覆盖行
docker exec dsa-server python -c "
import sqlite3
c = sqlite3.connect('/app/data/stock_analysis.db')
c.execute(\"DELETE FROM dsa_user_settings \"
          \"WHERE tenant_id=2 AND key='PUSH_TIMES'\")
c.commit()
print('已删除，剩余：', c.execute(\"SELECT tenant_id,key,value \"
      \"FROM dsa_user_settings WHERE tenant_id=2 AND key LIKE 'PUSH%'\").fetchall())
"
```

删掉行 = 回落到 `DEFAULT_PUSH_TIMES`（`["09:35","15:30"]`）。

