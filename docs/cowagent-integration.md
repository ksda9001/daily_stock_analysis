# CowAgent × daily_stock_analysis 接入指南

把 DSA 的分析能力接进 CowAgent，用微信做入口和出口。

```
        ┌──────────────┐   MCP (stdio)   ┌──────────────────┐   REST + Bearer   ┌─────────┐
用户 ──▶│   CowAgent   │◀───────────────▶│  dsa-mcp-server  │◀─────────────────▶│   DSA   │
微信 ◀──│ (编排 + 通道) │                 │   (本仓库 mcp_server) │   /api/v1/tenancy  │ (多租户) │
        └──────────────┘                 └──────────────────┘                   └─────────┘
```

- **CowAgent 拥有**：IM 通道（微信/企业微信/飞书…）、对话编排、Agent 记忆
- **DSA 拥有**：数据、分析、选股、决策信号、用户与配额
- **中间的 MCP 服务器**：只做协议翻译，无状态

> 本文讲**怎么接**。想看**生产上实际怎么部署的**（镜像怎么构建、容器怎么编排、
> 踩了哪些坑），看 [`docs/deployment-server.md`](./deployment-server.md)。
> 两篇的关系：本文是接入说明，那篇是运维手册。

---

## 1. 为什么非要用 CowAgent

一个容易忽略的事实：**DSA 自己推不到个人微信。**

DSA 的通知渠道里那个 `WECHAT_WEBHOOK_URL` 是**企业微信群机器人** webhook
（`qyapi.weixin.qq.com/cgi-bin/webhook/send`，见 `src/notification.py` 里
`NotificationChannel.WECHAT = "wechat"  # 企业微信`）。

个人微信只有 CowAgent 的 `weixin` 通道能到 —— 走的是**腾讯官方 API**
（扫码登录，机器人以「微信ClawBot」这个独立联系人出现，要求微信 8.0.69+）。
所以「通知经 CowAgent 发到微信」不是绕路，是唯一可行路径。

---

## 2. DSA 侧准备

### 2.1 打开多用户

```bash
DSA_MULTIUSER_ENABLED=true
```

默认是 `false`；关闭时整条链路 no-op，行为与上游完全一致。
详见 [`docs/multi-tenancy.md`](./multi-tenancy.md)。

### 2.2 建用户

```bash
# 用管理员账号登录后创建
curl -X POST http://127.0.0.1:8000/api/v1/tenancy/users \
  -H "Authorization: Bearer <管理员Token>" \
  -H "Content-Type: application/json" \
  -d '{"username":"alice","password":"<强密码>","display_name":"Alice"}'
```

### 2.3 给每个用户签发 Token

```bash
curl -X POST http://127.0.0.1:8000/api/v1/tenancy/auth/token \
  -H "Content-Type: application/json" \
  -d '{"username":"alice","password":"<强密码>"}'
# → {"token":"v1.3.1.1793...","token_type":"Bearer","user":{...}}
```

Token 默认 30 天有效。撤销方式：改密码，或
`POST /api/v1/tenancy/users/{id}/revoke-tokens`（会自增 `token_version`，
所有旧 Token 立即失效）。

> Token 等价于账号密码，**不要**写进仓库、不要贴到聊天里。

---

## 3. 部署 MCP 服务器

```bash
cd <daily_stock_analysis 仓库根目录>
pip install -r mcp_server/requirements.txt
```

### 3.1 先自检

MCP 服务器支持两种凭据模式（`mcp_server/client.py`）：

| 模式 | 环境变量 | 说明 |
|---|---|---|
| **Token**（推荐） | `DSA_API_TOKEN` | 用 `POST /api/v1/tenancy/auth/token` 事先签发，请求时直接带 |
| **账号密码** | `DSA_USERNAME` + `DSA_PASSWORD` | 首次请求时**惰性换取** Token，之后缓存 |

给**人**用（每人一个账号）建议用 Token；给**服务账号**用（如 CowAgent 这种
一个实例跑一个身份的）用账号密码更省事 —— 密码改了会自动重新换取。

```bash
# 模式一：Token
DSA_BASE_URL=http://127.0.0.1:8000 \
DSA_API_TOKEN=<alice 的 token> \
python -m mcp_server --check

# 模式二：账号密码
DSA_BASE_URL=http://127.0.0.1:8000 \
DSA_USERNAME=alice DSA_PASSWORD=<强密码> \
python -m mcp_server --check
```

期望输出（stderr）：

```
[*] 目标: http://127.0.0.1:8000
[✓] 连接与鉴权正常
    账号: alice (id=3, role=user)
    自选股: 4 只 (来源=user)
```

**这一步不通就不要往下走** —— 后面 CowAgent 报的错都会比这里难查。

> `--check` 需要**真实凭据**，所以**不能**放进 Dockerfile 的构建步骤里
> （构建期没有凭据，会直接 exit 2）。镜像里只做 `import` 冒烟测试，
> 真正的连通性验证放在容器起来之后。

---

## 4. 配置 CowAgent

CowAgent 读 `~/cow/mcp.json`，格式与 Claude Desktop / Cursor 基本一致。

**生产上实际用的配置**（Docker 部署，DSA 与 CowAgent 同在一个 `dsa-network`）：

```json
{
  "mcpServers": {
    "dsa": {
      "command": "/usr/local/bin/python",
      "args": ["-m", "mcp_server"],
      "env": {
        "PATH": "/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin",
        "PYTHONPATH": "/opt/dsa-mcp",
        "PYTHONUNBUFFERED": "1",
        "DSA_BASE_URL": "http://dsa-server:8000",
        "DSA_USERNAME": "cowagent",
        "DSA_PASSWORD": "<服务账号密码>",
        "DSA_MCP_LOG_LEVEL": "INFO"
      },
      "tool_name_prefix": "dsa_"
    }
  }
}
```

### 四个必须注意的点

**① `env` 必须显式写，一个都不能少。**
子进程**不会**继承你 `export` 的环境变量 —— 实测过：父进程设了
`DSA_BASE_URL`，服务器启动后仍打印默认的 `http://127.0.0.1:8000`。
更隐蔽的是 **`PATH` 也要写**：不写的话子进程可能找不到 `python`
（或者说找不到你 `command` 里那个解释器依赖的任何东西）。

**② CowAgent 的 `mcp.json` 没有 `cwd` 字段。**
这是个坑。`command: "python"` + `args: ["-m", "mcp_server"]` 依赖**工作目录
是仓库根目录**，但 CowAgent 不提供设置工作目录的字段 —— 写进去会被忽略。

所以 `python -m mcp_server` 必须靠 **`PYTHONPATH`** 来找到包：

```
"env": { "PYTHONPATH": "/opt/dsa-mcp" }     # mcp_server/ 的父目录
```

（`server.py` 也处理了「作为脚本直接运行」的情况，会自己修正 `sys.path`，
但那只在 `args: ["/abs/path/server.py"]` 这种写法下生效。）

**③ 加 `tool_name_prefix`。**
29 个工具里有 `get_watchlist`、`get_usage_summary` 这种通用名，很容易和
CowAgent 内置工具或其他 MCP 服务器撞名。加 `dsa_` 前缀后工具名变成
`dsa_add_to_watchlist`，不会冲突。

**④ 容器内互访用容器名，不要用 `127.0.0.1`。**
CowAgent 容器里的 `127.0.0.1` 是它自己，不是 DSA。同网络下直接写服务名：
`http://dsa-server:8000`。前提是两个容器在**同一个 docker network** 里。

> 改完 `mcp.json` 后**下一条消息**才会生效（CowAgent 有热重载）。
> Docker 部署：宿主机 `./cow` 挂到容器内 `/home/agent/cow`，把 `mcp.json`
> 丢进宿主机 `./cow/` 即可，**不用重启容器**。

### 验证 MCP 真的通了

看 CowAgent 启动日志（`docker logs cowagent | grep MCP`）：

```
[MCP] Server 'dsa' ready — 29 tool(s)
1/1 server(s) ready, 29 tool(s) available
```

`29 tool(s)` 就是对的。如果只有 `0`，说明子进程没起来 —— 九成是 ① 或 ②。

---

## 5. 工具清单（29 个）

| 能力 | 工具 |
|---|---|
| 身份 | `whoami`、`get_my_settings`、`update_my_settings` |
| **分析** | `analyze_stocks`、`get_analysis_task`、`list_analysis_history`、`get_analysis_report`、`get_report_markdown` |
| **添加股票** | `get_watchlist`、`add_to_watchlist`、`remove_from_watchlist`、`replace_watchlist` |
| **选股** | `list_screening_strategies`、`run_screening`、`get_screening_history`、`get_market_hotspots` |
| **问股** | `ask_stock_question`、`list_chat_sessions`、`get_chat_messages` |
| **AI 建议** | `get_decision_signal`、`list_decision_signals`、`run_deep_research` |
| **用量监控** | `get_my_usage`、`get_usage_summary` |
| 行情辅助 | `get_stock_quote`、`get_stock_profile` |

加了 `tool_name_prefix: "dsa_"` 后，实际暴露的名字是 `dsa_analyze_stocks` 等。

### ⚠️ 自选股为什么不用上游接口

上游的 `POST /api/v1/stocks/watchlist/add` 写的是**进程级全局** `STOCK_LIST`
（经 `SystemConfigService` 落到系统配置）。在多租户下用它会**串号**：
alice 加一只自选，bob 也跟着变。

所以 MCP 工具走的是 `/api/v1/tenancy/watchlist`（按用户存储）。
`tools.py` 里有测试专门断言这一点，防止以后有人「顺手改回上游接口」。

还有一个细节：用户**第一次**加自选时，会先继承全局列表再追加 ——
否则「加一只」会把继承来的整份自选替换成这一只。

---

## 6. 隔离模型：Agent 即租户

每个 DSA 用户对应**一份独立的 `mcp.json` 条目 + 一个独立 Token**。

DSA 侧不需要信任调用方传来的任何用户标识 —— 身份完全由 Token 决定，
服务端据此设置 `tenant_id`，数据隔离在 SQLAlchemy 会话层强制生效。
**即使 LLM 被 prompt injection 诱导去查别人的数据，也查不到。**

### CowAgent 支持「每个 Agent 独立 MCP 配置」吗？——**支持**（已实测源码）

这条之前标注为「未实测」，现在已在容器里读了实现，结论明确。

关键在 `common/state_dir.py` 的 `_shared_or_own()`：

```python
def _shared_or_own(identity, base, *parts):
    own = _agent_base(identity, base).joinpath(*parts)   # <该 Agent workspace>/mcp.json
    if own.exists():
        return own                                       # 有就用自己的
    return shared_root().joinpath(*parts)                # 没有就回落共享的
```

设计意图写得很直白（`tool_manager.py` 类注释）：

> One instance per Agent workspace. ... A single process-wide instance would
> let the first Agent to start decide which MCP servers exist, **hand its tools
> (and their credentials) to every other Agent**, and leave their own servers
> permanently unloaded.

也就是说：

- `ToolManager` **每个 Agent workspace 一个实例**，各自加载自己的 `mcp.json`；
- **opt-in 方式是「文件存在」而非「配置开关」**：某个 Agent 需要私有 MCP，
  就在**它自己的 workspace 下**放一个 `mcp.json`；没有就继续读共享的那份；
- 默认 Agent 的 workspace 就是共享根，所以**单 Agent 部署下两者是同一个路径**，
  不需要额外配置。

于是「一个微信用户 = 一个 DSA 账号」是**可以做到**的：
给每个用户建一个 CowAgent Agent，在各自 workspace 下放指向自己 Token 的 `mcp.json`。

> ⚠️ **一个会把人带偏的坑**：默认 Agent 的 workspace 是运行时用户的 `$HOME/cow`。
> 这个镜像的 entrypoint 会 `su agent` **降权**后再启动主进程，所以实际是
> `/home/agent/cow`。但如果你用 `docker exec`（默认 root）进去看，
> `HOME=/root`，解析出来是 `/root/cow` —— **那个目录根本不存在**，
> 会让人误以为配置放错了位置。
>
> 排查时务必带 `-u agent`：
> ```bash
> docker exec -u agent cowagent python -c \
>   "import sys;sys.path.insert(0,'/app');from common.state_dir import mcp_config_file as f;print(f())"
> # → /home/agent/cow/mcp.json
> ```

### 另外两个从源码里翻出来的有用选项

| 选项 | 位置 | 作用 |
|---|---|---|
| `inherit_full_env: true` | `mcp.json` 里**每个 server** 一项 | 恢复完整环境变量继承（默认只传安全变量 + 你写的 `env`，避免把 Agent 自己的 API Key 泄给子进程）。**敏感名仍会被剔除** |
| `mcp_stdio_command_allowlist` | CowAgent 的 `config.json` | 限制 stdio MCP 能拉起哪些可执行文件（如 `["python","npx"]`）。**默认空 = 不限制**，为兼容既有配置 |

如果嫌 `env` 里连 `PATH` 都要手写太啰嗦，可以改用 `inherit_full_env: true` —— 但要多想一步：
这样 MCP 子进程就能看到 CowAgent 自己的 `DEEPSEEK_API_KEY` 等变量了。

---

## 7. 微信通知怎么走

### 方案 A：Agent 主动拉取（推荐，零新增代码）

让 CowAgent 定时（或由用户一句「发我今天的分析」触发）：

1. 调 `dsa_list_analysis_history` 拿今天的报告列表；
2. 调 `dsa_get_analysis_report` 取正文；
3. 用自己的 `weixin` 通道发出去。

好处：IM 通道的所有权始终在 CowAgent 手里，DSA 侧一行代码都不用加。

### 方案 B：DSA 主动推送（需要一个小转发服务）

把用户的 `CUSTOM_WEBHOOK_URLS` 指向一个转发端点，由它调用 CowAgent 的消息接口。

⚠️ **CowAgent 的入站消息 API 我没有核实**（文档只讲了 MCP 和通道配置，
没讲 HTTP 入站接口）。走这条路前请先确认它有没有对外发消息的 API。

### 也可以两者并存

DSA 自己的 12 个渠道（企业微信机器人、飞书、Telegram、邮件、ntfy…）
都是**按用户可配**的（`update_my_settings` 就能改）。
个人微信走 CowAgent，企业微信群机器人继续走 DSA 原生渠道，互不冲突。

### 微信通道的几个事实

- 走**腾讯官方 API**，不是逆向协议 —— 不存在「封号风险」那套说法
- 机器人以独立联系人「**微信ClawBot**」出现，不影响正常使用
- 会话过期（errcode `-14`）会自动清凭据并重新出二维码，无需人工干预
- 要求微信客户端 **8.0.69+**（旧版本扫码后不出现联系人）

**凭据落在哪 —— 取决于有没有设 `COW_DATA_DIR`：**

| `COW_DATA_DIR` | 凭据路径 | 容器重建后 |
|---|---|---|
| **已设**（推荐） | `<COW_DATA_DIR>/weixin_credentials.json` | 在挂载卷里，**免扫码** |
| 未设 | `~/.weixin_cow_credentials.json` | 在容器可写层，**重建即丢，要重扫** |

本项目的部署设了 `COW_DATA_DIR=/home/agent/.cow` 并挂载了 `./cow-data`，
所以实际路径是：

```
/home/agent/.cow/weixin_credentials.json   →  宿主机 ./cow-data/weixin_credentials.json
```

验证（未扫码时不存在是正常的）：

```bash
docker exec -u agent cowagent python -c \
  "import sys;sys.path.insert(0,'/app');from config import get_weixin_credentials_path as g;print(g())"
```

> ⚠️ **首次登录的二维码不是「无限自动刷新」的**（实测
> `channel/weixin/weixin_channel.py`）：
>
> ```
> QR_LOGIN_TIMEOUT_S = 480     # 整个登录窗口 8 分钟
> QR_MAX_REFRESHES   = 10      # 最多刷新 10 次
> ```
>
> 二维码本身约 2 分钟过期、会自动换新的，但**刷新满 10 次或总时长到 480s 后，
> 通道会放弃**（日志：`QR login timed out` / `二维码登录超时，请通过控制台重新接入`），
> 此时**必须重启容器**才会重新开一个窗口。
>
> 所以**不要**提前截图放着 —— 要扫的时候现取。
> 取码脚本会自动处理「窗口已死」的情况（检测到超时就重启容器开新窗口）：
> 见 [`docs/deployment-server.md`](./deployment-server.md) §8「控制台访问」。

---

## 8. 故障排查

| 现象 | 排查 |
|---|---|
| CowAgent 里看不到任何 `dsa_*` 工具 | `~/cow/mcp.json` 是否存在且是合法 JSON；改动是否在下一条消息后才生效 |
| `mcp.json` 明明放了却没生效 | **路径不对**。它是相对「该 Agent 的 workspace」解析的，不是相对任意 `~`。用 `docker exec -u agent` 打印 `mcp_config_file()` 核对真实路径（见 §6 的坑） |
| 工具报 `missing_credentials` | `env` 里没写 `DSA_API_TOKEN`，或没写 `DSA_USERNAME`+`DSA_PASSWORD`（**子进程不继承父进程环境变量**） |
| 子进程起不来 / `command not found` | `env` 里漏了 `PATH`（默认不继承完整环境） |
| `No module named mcp_server` | 漏了 `PYTHONPATH`，或写错了目录（应为 `mcp_server/` 的**父目录**） |
| 工具报 `connection_error` | `DSA_BASE_URL` 写错，或 DSA 没起。**容器内要用服务名** `http://dsa-server:8000`，不能用 `127.0.0.1`；并确认两容器在同一 network |
| 报 `multiuser_disabled` | DSA 没开 `DSA_MULTIUSER_ENABLED=true` |
| 报 `invalid_credentials` | 密码错，或用户被禁用 |
| 报 401 但 Token 刚签发 | Token 被撤销（改密码 / revoke-tokens 会自增 `token_version`） |
| 工具返回「已截断」 | 正常行为。报告太长会截断到 24000 字符，用 `max_chars` 调大 |
| 自选股加不上 | 确认走的是 `dsa_add_to_watchlist`，不是上游的全局接口 |
| 服务器启动即崩 | 看 CowAgent 日志里的 `[MCP] Server 'dsa' load failed`；先在命令行跑 `--check` |

排障顺序固定：**先在命令行 `--check`，再查 CowAgent 配置**。
命令行不通就与 CowAgent 无关。

想直接看 MCP 有没有起来，最省事的一条命令：

```bash
docker logs cowagent 2>&1 | grep -i mcp
# 期望： [MCP] Server 'dsa' ready — 29 tool(s)
#        [ToolManager] MCP loading complete: 1/1 server(s) ready, 29 tool(s) available
```

---

## 9. 一键联调自检

单测各自只验证了一半 —— 租户测试不经过 MCP，MCP 测试不连真实 DSA。
`scripts/e2e_mcp_chain.py` 补上那一环：**真的**起一个 DSA、**真的**用 stdio
拉起 MCP 服务器、**真的**带 Bearer Token 走 HTTP 调过去，然后断言两个用户互相看不到对方的数据。

```bash
python scripts/e2e_mcp_chain.py
python scripts/e2e_mcp_chain.py --verbose   # 失败时自动打印服务端日志
```

它会自动开一个临时库和随机端口，跑完即清理，**不碰你的正式数据**。
23 项断言，覆盖：

| 分组 | 断言内容 |
|---|---|
| ⓪ 启动 | DSA 以多用户模式启动并健康 |
| ① 账号 | 管理员设密码、建 alice/bob、各自签发 Token |
| ② 身份 | MCP 工具全部注册；Token 正确解析为对应用户；写入落到「个人」而非「全局」 |
| ③ **隔离** | **bob 看不到 alice 的自选；alice 看不到 bob 的；一方的增删不影响另一方** |
| ④ 用量 | 用量归属到本人 `tenant_id` |
| ⑤ 安全 | 非法代码被拒且不崩；伪造 Token 被拒 |

改动 MCP 或租户代码后，**先跑这个再提交**。

> 首次运行需要 DSA 的依赖装全（尤其 `litellm`）——
> 它是真实启动服务，不像单测那样 mock 掉重依赖。

---

## 10. 已验证 / 未验证

诚实划一下边界，免得踩坑时找错方向。

**已实测（本地，无 CowAgent 环境）：**

- **MCP → DSA 全链路（23/23 断言通过）**：真实服务 + 真实 stdio + 真实 HTTP + 真实 Token
- **跨用户隔离在 MCP 层依然成立**：bob 拿自己的 Token 看不到 alice 的自选
- MCP 服务器与标准客户端完成 stdio 握手，29 个工具全部注册成功
- `tools/list`、`tools/call` 正常；工具失败返回结构化 JSON 而非协议错误
- 子进程**不继承**父进程环境变量（所以 `env` 必须显式写）
- 中文内容经 stdio 往返无损
- 每个工具的「方法 + 路径 + 载荷」都有测试覆盖（40 个用例）
- 自选股确实走按用户端点，不会写到全局配置

**已实测（生产服务器，CowAgent 真实部署）：**

- **CowAgent 侧 `[MCP] Server 'dsa' ready — 29 tool(s)`**，
  `1/1 server(s) ready, 29 tool(s) available` —— MCP 子进程真实拉起、握手成功
- **容器内 stdio 端到端 6/6 通过**：`whoami` → `cowagent`；
  `get_watchlist` 继承到全局列表；`get_my_usage` 归属到 `tenant_id: 2`
- **HTTP 侧多租户 12/12 通过**：签发 Token、`/auth/me`、自选增删、
  用量归属、无 Token → 401、非管理员 → 403
- **`mcp.json` 的 workspace 解析**（读源码 + 运行时打印双重确认）
- **微信凭据路径**：`COW_DATA_DIR` 已设 → `/home/agent/.cow/weixin_credentials.json`
- **`env` 不写 `PATH` 会起不来**、**没有 `cwd` 字段**（只能靠 `PYTHONPATH`）
- CowAgent **支持每个 Agent 独立 `mcp.json`**（源码确认，见 §6）

**仍未实测（需你确认）：**

- **微信通道本身还没登录** —— 二维码已生成待扫；扫码后
  「机器人是否出现在微信联系人里、能否收发消息」需要你实际试一下
- CowAgent 的**入站消息 API**（§7 方案 B 的前提）—— 官方文档没写，
  没去翻源码确认它有没有对外发消息的 HTTP 接口
- **多 Agent 各自独立 MCP 配置**虽然源码支持，但**没实际建第二个 Agent 验证过**
  （单 Agent 部署下走的是共享回落分支）

---

> 生产部署的完整拓扑、镜像构建、回滚步骤、以及**绝对不能碰的 Tailscale 策略路由**，
> 见 [`docs/deployment-server.md`](./deployment-server.md)。
