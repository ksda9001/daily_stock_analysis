# 行情推送特性 · 部署手册

> 特性：每天两次从 CowAgent 向用户推送「大盘 + 自选股」行情，用户可取消或改时间。
> 设计日期 2026-09-14。**本文件是部署的唯一依据**，执行前先读「回滚」一节。

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

**回滚不需要重扫微信二维码** —— 状态在两个 bind mount 里，凭据路径不含容器标识。

## 4. 风险与注意

- ⚠️ 重建 `dsa-server` 会**短暂停机**（数十秒），`trader01` 会受影响
- ⚠️ 改 `overrides/` 里的文件**必须就地改写**（`cat tmp > target`），不能 `mv`
  —— `-v` 绑的是 inode 不是路径
- ⚠️ `.dockerignore` 与 `Dockerfile` 是一对，构建失败先查它
- ⚠️ 服务器**不能 push**，只能 `git fetch` + `reset --hard`
- 安全遗留：`docker logs cowagent` 每次启动明文打印 `web_password`（与 root SSH 同口令）、
  `mcp.json` 里 `DSA_PASSWORD` 明文 —— 与本次部署无关，但应尽快轮换
