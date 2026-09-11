# DSA MCP 服务器

把 daily_stock_analysis 的 REST API 包装成 [MCP](https://modelcontextprotocol.io)
工具，供 CowAgent 等 Agent 框架通过 **stdio** 调用。

完整接入步骤见仓库根目录的 [`docs/cowagent-integration.md`](../docs/cowagent-integration.md)，
本文件只讲这一层。

## 装依赖

```bash
pip install -r mcp_server/requirements.txt
```

`mcp` SDK 1.x 用 `FastMCP`，2.x 改名为 `MCPServer`。`server.py` 做了兼容导入，
两个大版本都能跑，所以这里不锁死上限。

## 配置

两个环境变量：

| 变量 | 说明 |
|---|---|
| `DSA_BASE_URL` | DSA 服务地址，默认 `http://127.0.0.1:8000` |
| `DSA_API_TOKEN` | Bearer Token，用 `POST /api/v1/tenancy/auth/token` 签发 |

也可以用 `DSA_USERNAME` + `DSA_PASSWORD` 代替 Token（首次请求时惰性换取）。

## 自检

```bash
export DSA_BASE_URL=http://127.0.0.1:8000
export DSA_API_TOKEN=<token>

python -m mcp_server --check
```

会依次验证「能否连上 → Token 是否有效 → 对应哪个账号 → 自选股读得到吗」，
全部输出到 stderr。**排障先跑这个。**

## 启动

```bash
python -m mcp_server          # 以 stdio 启动
python mcp_server/server.py   # 等效写法
```

## 结构

| 模块 | 职责 | 依赖 MCP SDK |
|---|---|---|
| `client.py` | HTTP 传输：鉴权、错误映射、输出截断 | 否 |
| `tools.py` | 工具的业务实现（纯函数） | 否 |
| `server.py` | MCP 协议层：注册工具、stdio 启动 | 是 |

前两层不依赖 MCP SDK，因此单元测试可以直接调用，不需要起 MCP 会话；
换 Agent 框架时业务逻辑也不用重写。

## 两个容易踩的坑

1. **stdout 是 JSON-RPC 通道。** 任何 `print` 或输出到 stdout 的日志都会破坏协议，
   表现为客户端报「解析失败」。本模块的日志一律走 stderr。
2. **子进程不继承父进程的环境变量。** MCP 客户端拉起服务器时通常给一个干净环境，
   所以 `DSA_BASE_URL` / `DSA_API_TOKEN` 必须在客户端配置里**显式**写在 `env` 字段，
   而不是指望 `export` 生效。
