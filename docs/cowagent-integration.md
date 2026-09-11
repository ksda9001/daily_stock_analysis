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

```bash
DSA_BASE_URL=http://127.0.0.1:8000 \
DSA_API_TOKEN=<alice 的 token> \
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

---

## 4. 配置 CowAgent

CowAgent 读 `~/cow/mcp.json`，格式与 Claude Desktop / Cursor 一致。

```json
{
  "mcpServers": {
    "dsa": {
      "command": "python",
      "args": ["-m", "mcp_server"],
      "env": {
        "DSA_BASE_URL": "http://127.0.0.1:8000",
        "DSA_API_TOKEN": "<alice 的 token>"
      },
      "tool_name_prefix": "dsa_"
    }
  }
}
```

### 三个必须注意的点

**① `env` 必须显式写。**
子进程**不会**继承你 `export` 的环境变量 —— 实测过：父进程设了
`DSA_BASE_URL`，服务器启动后仍打印默认的 `http://127.0.0.1:8000`。
所以 Token 和地址只能写在 `env` 里。

**② 加 `tool_name_prefix`。**
26 个工具里有 `get_watchlist`、`get_usage_summary` 这种通用名，很容易和
CowAgent 内置工具或其他 MCP 服务器撞名。加 `dsa_` 前缀后工具名变成
`dsa_add_to_watchlist`，不会冲突。

**③ `cwd` 要对。**
`command: "python"` + `args: ["-m", "mcp_server"]` 依赖**工作目录是仓库根目录**。
如果 CowAgent 不保证这一点，改用绝对路径：

```json
{
  "command": "/usr/bin/python3",
  "args": ["/abs/path/to/daily_stock_analysis/mcp_server/server.py"]
}
```

`server.py` 已处理「作为脚本直接运行」的情况（会自己修正 `sys.path`）。

> 改完 `mcp.json` 后**下一条消息**才会生效（CowAgent 有热重载）。
> Docker 部署：宿主机 `./cow` 会挂到容器内 `/home/agent/cow`，把 `mcp.json`
> 丢进宿主机 `./cow/` 即可。

---

## 5. 工具清单（26 个）

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

### ⚠️ 需要你确认的一件事

CowAgent 的 `mcp.json` 是**全局**的（`~/cow/mcp.json`）。这意味着：

- 如果 CowAgent 只有**一个 Agent 实例**，那所有微信用户共用同一个 Token
  → 实际上退化成单用户，所有人都看到同一个账号的数据；
- 要做到「一个微信用户 = 一个 DSA 账号」，需要**每个用户一个 CowAgent 实例**，
  各自有自己的 `~/cow/mcp.json`。

**这一点我没有实测**（本仓库没有 CowAgent 环境，我只核对了它的官方文档）。
请在接入前确认 CowAgent 是否支持「每个 Agent 独立配置 MCP」。
若不支持，可选方案：多实例部署，或用一层薄路由按用户转发。

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

### 微信通道的几个事实（来自官方文档）

- 走**腾讯官方 API**，不是逆向协议 —— 不存在「封号风险」那套说法
- 机器人以独立联系人「**微信ClawBot**」出现，不影响正常使用
- 凭据存 `~/.weixin_cow_credentials.json`，重启免扫码
- 会话过期（errcode `-14`）会自动清凭据并重新出二维码，无需人工干预
- 要求微信客户端 **8.0.69+**

---

## 8. 故障排查

| 现象 | 排查 |
|---|---|
| CowAgent 里看不到任何 `dsa_*` 工具 | `~/cow/mcp.json` 是否存在且是合法 JSON；改动是否在下一条消息后才生效 |
| 工具报 `missing_credentials` | `env` 里没写 `DSA_API_TOKEN`（**子进程不继承父进程环境变量**） |
| 工具报 `connection_error` | `DSA_BASE_URL` 写错，或 DSA 没起；先在宿主机 `curl` 一下 |
| 报 `multiuser_disabled` | DSA 没开 `DSA_MULTIUSER_ENABLED=true` |
| 报 `invalid_credentials` | 密码错，或用户被禁用 |
| 报 401 但 Token 刚签发 | Token 被撤销（改密码 / revoke-tokens 会自增 `token_version`） |
| 工具返回「已截断」 | 正常行为。报告太长会截断到 24000 字符，用 `max_chars` 调大 |
| 自选股加不上 | 确认走的是 `dsa_add_to_watchlist`，不是上游的全局接口 |
| 服务器启动即崩 | 看 CowAgent 日志里的 `[MCP] Server 'dsa' load failed`；先在命令行跑 `--check` |

排障顺序固定：**先在命令行 `--check`，再查 CowAgent 配置**。
命令行不通就与 CowAgent 无关。

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

**已实测：**

- **MCP → DSA 全链路（23/23 断言通过）**：真实服务 + 真实 stdio + 真实 HTTP + 真实 Token
- **跨用户隔离在 MCP 层依然成立**：bob 拿自己的 Token 看不到 alice 的自选
- MCP 服务器与标准客户端完成 stdio 握手，26 个工具全部注册成功
- `tools/list`、`tools/call` 正常；工具失败返回结构化 JSON 而非协议错误
- 子进程**不继承**父进程环境变量（所以 `env` 必须显式写）
- 中文内容经 stdio 往返无损
- 每个工具的「方法 + 路径 + 载荷」都有测试覆盖（40 个用例）
- 自选股确实走按用户端点，不会写到全局配置

**未实测（需你确认）：**

- CowAgent 本身没跑过 —— `mcp.json` 格式来自其官方文档，非实测
- CowAgent 是否支持「每个 Agent 独立 MCP 配置」（决定能否做到一用户一租户）
- CowAgent 的入站消息 API（方案 B 的前提）
- DSA 在远程沙箱上的实际连通性
