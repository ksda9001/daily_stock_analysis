# 服务器部署手册（DSA fork + CowAgent）

本文记录 **172.245.211.211**（SSH 端口 **882**）上这套环境的真实拓扑、部署步骤、
已验证事实与回滚方式。所有内容都来自实际执行与核对，不是设想。

> 换机器部署前请先读 §2「环境约束」——那里有几条会直接决定成败的硬约束。

---

## 1. 目标拓扑

```
                    ┌─────────────────────────────────────────┐
   Cloudflare ──443─┤ nginx                                   │
   (fi.myfi.cc.cd)  │  ├─ / → 127.0.0.1:8000  (DSA fork)      │
                    └─────────────────────────────────────────┘
                                        │
        ┌───────────────────────────────┴──────────────────┐
        │ dsa-network (172.18.0.0/16)                      │
        │  ├─ dsa-server   (dsa-fork:deploy)   :8000       │
        │  ├─ searxng      (searxng/searxng)   :8080       │
        │  └─ cowagent     (cowagent-dsa)      :9899       │
        │         └─ 子进程: mcp_server (stdio, 26 工具)    │
        └──────────────────────────────────────────────────┘
```

- **DSA** 提供业务 API + 多租户隔离，nginx 暴露为 `https://fi.myfi.cc.cd`。
- **CowAgent** 是 Agent 运行时，通过 **MCP over stdio** 调用 DSA 的 26 个工具。
- 两者在**同一个 docker 网络**里，所以 MCP 用容器名 `http://dsa-server:8000` 直连，
  不依赖任何硬编码 IP。
- **「Agent 即租户」**：CowAgent 用独立账号 `cowagent`（`tenant_id=2`）访问 DSA，
  它有自己的自选股、用量统计与分析历史，与真人用户互不可见。

---

## 2. 环境约束（决定成败的硬约束）

| 约束 | 事实 | 影响 |
|---|---|---|
| **内存** | 1.98 GB 物理 + 2 GB swap | 只够跑**一个** DSA 实例。实测 `dsa-server` 500 MB、`cowagent` 130 MB、`searxng` 28 MB |
| **端口白名单** | 机房只放行 **80 / 443 / 882**，8000、9899 等一律超时 | 任何新服务都必须经 nginx 走 443；不要指望直连端口 |
| **DNS** | `myfi.cc.cd` 在 Cloudflare，**无通配符**，`fi` 记录指向 Cloudflare 代理 | 新增控制台域名需要先加 DNS 记录 |
| **策略路由** | Tailscale 出口节点 + `table 52`（4303 条国内网段） | **见 §7，别碰** |
| **上游镜像代码较旧** | 官方镜像 `main.py` 65662 B，fork base `main.py` 68887 B | 不能「拿旧镜像套新源码」，但可以「旧镜像 + 新源码」——见 §3.1 的依据 |
| **templates/ 未被官方 Dockerfile 拷贝** | 镜像内不存在 `/app/templates` | 线上报告一直走 `render()` 返回 `None` 后的兜底渲染。**拷入会改变报告格式**，属行为变更，不要顺手加 |

---

## 3. 部署 DSA（fork）

### 3.1 为什么用「轻量构建」而不是官方 Dockerfile

官方 `docker/Dockerfile` 是多阶段构建：`node:20-slim` 编前端 + `python:3.11-slim` 装依赖。
在只有 ~280 MB 可用内存的机器上跑 `npm ci` + `pip install` 有实际 OOM 风险。

我们逐项核实过以下事实，因此可以安全地**以官方镜像为底、只覆盖 Python 源码**：

1. fork 相对上游 base **未改动** `apps/` 与 `static/`（前端），可直接复用镜像内已编译的 `/app/static`；
2. fork 的 `requirements.txt` 与镜像内**只差一行** `litellm` 上界，
   而镜像内 `litellm==1.98.0` 同时满足两边的钉版——**零新增依赖**；
3. fork 的改动全部集中在 Python 源码。

结果：构建耗时 **12 秒**，服务器零额外负担。

### 3.2 构建

```bash
mkdir -p /root/dsa-fork && cd /root/dsa-fork
git clone --depth 1 https://github.com/ksda9001/daily_stock_analysis.git repo
cp /root/FI-backups/$(cat /root/FI-backups/LATEST)/app.env app.env
echo "DSA_MULTIUSER_ENABLED=true" >> app.env          # 开启多租户
docker build -t dsa-fork:deploy -f Dockerfile.patch .
```

`Dockerfile.patch` 的内容（含逐条依据注释）见服务器 `/root/dsa-fork/Dockerfile.patch`。

> **`app.env` 为什么必须带进镜像？**
> 线上 DSA 的配置**唯一来源**是容器可写层里的 `/app/.env`——它由 WebUI
> 保存配置时写出（`os.replace` 原子更新），**不在官方镜像里**。
> 生产容器的 `docker inspect .Config.Env` 只有 11 个镜像默认变量，没有任何业务配置。
> 所以重建容器时若不带上这个文件，会得到一个「未配置 STOCK_LIST / 未配置任何可用的
> AI 模型接入」的空壳实例。已实测踩过。

### 3.3 ⚠️ overlay 遗留表必须先让路

**这一步不做，容器起不来。**

数据库里残留着上一代 overlay 方案建的三张表，**表名与我们的冲突但结构不同**：

| 表 | overlay 结构 | 我们的结构 |
|---|---|---|
| `dsa_users` | `password_salt` / `is_active` | `status` / `is_system` / `token_version` |
| `dsa_user_settings` | PK `user_id` | PK `id` + `tenant_id` |
| `dsa_invites` | 仅 overlay 有 | 无此表 |

`ensure_tenancy_schema()` 的第一步是 `if not inspector.has_table(name)` ——
表已存在就**不会重建**，紧接着 `_seed_system_owner()` 会
`SELECT is_system, status, token_version` 直接抛 `no such column`，
而多租户引导失败是**直接抛异常拒绝启动**的（安全设计：半吊子状态比不启动更危险）。

处理脚本（改名保留数据 + 清掉遗留索引）：

```python
# ALTER TABLE ... RENAME TO 不会重命名索引！遗留索引会与新表索引重名。
for t in ("dsa_users", "dsa_user_settings", "dsa_invites"):
    if 表存在:
        ALTER TABLE t RENAME TO t + "_legacy_overlay"
        for 索引 in 该表上的非自动索引:
            DROP INDEX 索引        # 例如 ix_dsa_users_username，不删会撞新表
```

完整脚本：服务器 `/tmp/rename_legacy.py`（同仓库 `scripts/` 无此文件，属运维动作）。

> 生产库里还留着 21 张表的 `owner_id` 列（overlay 注入的 TEXT 列）。
> 我们用的是 `tenant_id`（INTEGER），两者互不干扰，**先保留不动**。

### 3.4 ⚠️ 必须接回 `dsa-network`，否则新闻搜索失效

`SEARXNG_BASE_URLS=http://searxng:8080` 依赖 `dsa-network` 的容器名解析。
新建容器时如果只挂默认 `bridge`，`getent hosts searxng` 会 `DNS_FAIL`，
DSA 的新闻搜索能力静默失效。

```bash
docker network connect dsa-network dsa-server     # 运行中的容器可直接接，无需重建
```

**验证**：`docker exec dsa-server getent hosts searxng` 必须返回 IP。

### 3.5 启动

```bash
docker run -d --name dsa-server \
  --restart unless-stopped \
  -p 0.0.0.0:8000:8000 \
  -e TZ=Asia/Shanghai \
  -v /root/FI/data:/app/data \
  -v /root/FI/logs:/app/logs \
  -v /root/FI/reports:/app/reports \
  dsa-fork:deploy \
  python main.py --serve-only --host 0.0.0.0 --port 8000
docker network connect dsa-network dsa-server     # ← 别忘
```

启动日志里应能看到：

```
[tenancy] seeded system owner user id=1
[tenancy] schema bootstrap applied: {... 'warnings': [] ...}
[tenancy] tenant scope guard armed
[tenancy] tenancy API mounted at /api/v1/tenancy
```

`warnings` 必须为空数组。

---

## 4. 部署 CowAgent

### 4.1 构建带 MCP 的衍生镜像

官方镜像缺 `mcp` SDK，且 MCP server 代码需要随镜像分发（只放容器可写层会在容器重建时丢失）。

```bash
mkdir -p /root/cow-build
cp -r /root/dsa-fork/repo/mcp_server /root/cow-build/mcp_server
# Dockerfile: FROM zhayujie/chatgpt-on-wechat:latest
#             RUN pip install "mcp>=1.2" "httpx>=0.27"
#             COPY mcp_server/ /opt/dsa-mcp/mcp_server/
docker build -t cowagent-dsa:latest /root/cow-build
```

> 不在构建期跑 `python -m mcp_server --check`：那是**运行期**自检、需要 DSA 凭据，
> 构建时必然以退出码 2 失败。

### 4.2 项目目录与配置

```
/root/cowagent/
├── docker-compose.yml
├── .env                 # DEEPSEEK_API_KEY / WEB_PASSWORD（600 权限）
├── cow/mcp.json         # → 容器内 /home/agent/cow/mcp.json
├── cow-data/            # → 容器内 /home/agent/.cow（config.json、日志、微信凭据）
└── show-wechat-qr.sh
```

挂载目录属主必须是 **uid 999**（容器内 `agent` 用户）：

```bash
chown -R 999:999 /root/cowagent/cow /root/cowagent/cow-data
```

### 4.3 `mcp.json`

```json
{
  "mcpServers": {
    "dsa": {
      "command": "/usr/local/bin/python",
      "args": ["-m", "mcp_server"],
      "env": {
        "PATH": "/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin",
        "PYTHONPATH": "/opt/dsa-mcp",
        "DSA_BASE_URL": "http://dsa-server:8000",
        "DSA_USERNAME": "cowagent",
        "DSA_PASSWORD": "……",
        "DSA_MCP_LOG_LEVEL": "INFO"
      },
      "tool_name_prefix": "dsa_"
    }
  }
}
```

四个必须注意的点：

1. **`env` 必须显式写全**。MCP 子进程不保证继承父进程环境变量，实测过；
   `PATH` 也要给，否则 `python` 找不到。`command` 用绝对路径更稳。
   （嫌啰嗦可加 `"inherit_full_env": true` 恢复完整继承，但那样 CowAgent 自己的
   `DEEPSEEK_API_KEY` 等也会暴露给子进程——敏感名仍会被剔除。）
2. **`tool_name_prefix: "dsa_"`**，避免与 CowAgent 内置工具（`read`/`write`/`bash`…）撞名。
3. **不支持 `cwd` 字段**（官方字段只有 command/args/env/url/type/headers/scope/tool_name_prefix/disabled），
   所以靠 `PYTHONPATH` 让 `python -m mcp_server` 找到包。
4. **`DSA_BASE_URL` 用容器名**，不要写 `127.0.0.1`——那是 CowAgent 自己。

### 4.3.1 ⚠️ `mcp.json` 的解析基准是「Agent workspace」，排查时别被 `docker exec` 骗了

CowAgent 不是简单读 `~/cow/mcp.json`，而是 `common/state_dir.py::_shared_or_own()`
以**当前 Agent 的 workspace** 为基准解析：

```python
own = <agent workspace>/mcp.json
return own if own.exists() else shared_root()/mcp.json
```

默认 Agent 的 workspace 就是共享根，单 Agent 部署下两者同路径。但 **workspace 是
按运行时用户的 `$HOME/cow` 算的**：本镜像 entrypoint 会 `su agent` **降权**后再启动
主进程，所以真实值是 `/home/agent/cow`。

**坑在这里**：`docker exec` 默认用 **root**（`HOME=/root`），解析出来是 `/root/cow`——
而那个目录**根本不存在**，会让人误判「配置放错地方了」。

```bash
# ✗ 会被带偏
docker exec cowagent python -c "...mcp_config_file()"     # → /root/cow/mcp.json（不存在）

# ✓ 正确姿势
docker exec -u agent cowagent python -c \
  "import sys;sys.path.insert(0,'/app');from common.state_dir import mcp_config_file as f;print(f(), f().exists())"
# → /home/agent/cow/mcp.json True
```

**副产品**：既然解析基准是「Agent workspace」且 opt-in 方式是**文件存在**，
那么**每个 Agent 都可以有自己的 `mcp.json`** —— 这是「一个微信用户 = 一个 DSA 账号」
的实现路径（在其 workspace 下放指向自己 Token 的 `mcp.json`）。
`tool_manager.py` 的类注释明确说明该设计就是为了避免
「第一个 Agent 决定所有 Agent 有哪些 MCP、并把自己的凭据发给别人」。

### 4.4 微信凭据会落在挂载卷内（已验证）

`COW_DATA_DIR=/home/agent/.cow` 时，`config.py::get_weixin_credentials_path()`
返回 `<COW_DATA_DIR>/weixin_credentials.json`，即落在 `./cow-data` 挂载卷里，
**容器重建不会丢登录态**。若不设 `COW_DATA_DIR`，凭据会落到 `~/.weixin_cow_credentials.json`（容器可写层），重建即丢失。

### 4.5 启动与验收

```bash
cd /root/cowagent && docker compose up -d
docker logs cowagent | grep -E "MCP|ToolManager"
```

期望输出：

```
[MCP] Server 'dsa' ready — 26 tool(s): ['dsa_whoami', ..., 'dsa_get_stock_profile']
[ToolManager] MCP loading complete: 1/1 server(s) ready, 26 tool(s) available
```

---

## 5. 验收清单

在 **CowAgent 容器内**跑（这才是真实链路）：

```bash
docker exec -e DSA_PASSWORD="……" cowagent python /tmp/probe_mcp.py
```

`probe_mcp.py` 用 stdio 直连 MCP server 并真实调用工具。期望：

```
[PASS] MCP 握手成功  -> daily-stock-analysis
[PASS] 工具数量 = 26
[PASS] whoami 返回 cowagent
[PASS] get_watchlist 返回自选股
[PASS] get_my_usage 归属正确 -> tenant_id: 2
[PASS] list_screening_strategies 无异常
通过 6 / 6
```

DSA 侧的多租户 HTTP 验收（`/root/dsa-fork/tools/verify_api.py`）：

```bash
python3 /root/dsa-fork/tools/verify_api.py http://127.0.0.1:8000 cowagent "……"
# 期望 通过 12 / 12
```

> **判断 tenancy 是否真的挂上的正确方法**：对比
> `/api/v1/tenancy/capabilities`（应 200）与一个**不存在的路径**（应 401）。
> 只看前者会误判——认证中间件对未知路径也会返回 401。

---

## 6. 回滚

切换时旧容器被**停止但保留**，改名 `dsa-upstream-20260912`：

```bash
docker stop dsa-server && docker rm dsa-server
docker rename dsa-upstream-20260912 dsa-server
docker start dsa-server
```

数据库若要一并回滚，用切换前的快照（`/root/FI-backups/`，用 SQLite 在线备份 API 生成，非文件拷贝）：

```bash
BK=/root/FI-backups/<时间戳>
docker stop dsa-server
cp $BK/stock_analysis.db /root/FI/data/stock_analysis.db
rm -f /root/FI/data/stock_analysis.db-wal /root/FI/data/stock_analysis.db-shm
docker start dsa-server
```

> ⚠️ 数据库处于 **WAL 模式**，直接 `cp` 主库文件会拿到不一致快照。
> 必须用 `sqlite3.Connection.backup()` 或先停容器。

---

## 7. ⚠️ 不要碰的东西：Tailscale 策略路由

这台机器有一套**境内外分流**机制，由 Gemini 协助搭建，用于让国内数据源走国内宽带出口：

- 国内出口：家用路由器 `100.70.137.69`（电信，实测出口 IP `113.132.84.184`）
- `ip rule` 优先级 **5270** → `lookup 52`
- `table 52` 装载 4303 条国内网段，下一跳 `dev tailscale0`，**刻意剔除 `default`**
- `iptables -t mangle` 中 5 条 TCPMSS 钳制规则（`tailscale0` MTU 1280 vs `docker0` 1500）
- `/usr/local/bin/setup-china-routes.sh` + `china-routes.service`（enabled）
- `/etc/resolv.conf` 锁定 `8.8.8.8` / `8.8.4.4`

**严禁** `tailscale set --exit-node=...` 做全局默认路由——会让所有海外组件
（LLM API、SearXNG、Google）立刻撞 GFW。设计上只把国内网段指向 tailscale0。

**验证分流是否正常**（决定性）：

```bash
ip route get 223.5.5.5   # → dev tailscale0 table 52
ip route get 8.8.8.8     # → via <网关> dev eth0
curl -s http://ip.3322.net   # 国内 IP 承载的回显服务，应返回 113.132.x.x
```

> 别用 `myip.ipip.net` 验证——它解析到 **Cloudflare**，走的是海外出口，
> 会返回美国 IP，看起来像「代理挂了」，其实是测试方法错了。

---

## 8. 控制台访问

控制台在容器内是 `:9899`，但机房**只放行 80/443/882**，所以外网直连不通。
两种方式：

**A. SSH 隧道（立即可用，零配置）**

```bash
ssh -p 882 -L 9899:127.0.0.1:9899 root@172.245.211.211
# 然后浏览器打开 http://127.0.0.1:9899
```

**B. 独立子域名 + HTTPS（推荐长期使用）**

⚠️ **顺序不能反：DNS 先，证书后。** Let's Encrypt 的 HTTP-01 校验要求域名
先能解析到本机，所以没加 DNS 记录时 `certbot` 必然失败。

**第 1 步（必须在 Cloudflare 控制台手工做）**：

| 类型 | 名称 | 内容 | 代理状态 |
|---|---|---|---|
| `A` | `cow` | `172.245.211.211` | 橙色云（代理）或灰色云（仅 DNS）**都可以** |

> `myfi.cc.cd` 的 NS 是 Cloudflare（`edna.ns.cloudflare.com` / `rex.ns.cloudflare.com`），
> **没有通配符记录**，所以每条子域名都要单独加。

**第 2 步：一条命令完成证书扩展 + 重载**

```bash
bash /root/cowagent/fix-console-domain-cert.sh cow.myfi.cc.cd fi.myfi.cc.cd
```

它做四件事：验证域名**真的**能打到本机 nginx → `certbot --expand` 把 cow 并进
fi 的证书 lineage → `nginx -t` → `systemctl reload nginx`。

> **为什么用 `--expand` 而不是新签一张证书？**
> `fi` 和 `cow` 共用一张证书后，**现有 nginx 配置一行都不用改**——
> 它本来就指向 `/etc/letsencrypt/live/fi.myfi.cc.cd/`。`--expand` 保留 lineage 名，
> 只往 SAN 里加域名，证书路径不变。
> 若走「给 cow 单独签一张」，就得同步把配置里的 `ssl_certificate` 改成
> `live/cow.myfi.cc.cd/`，多一个容易忘的步骤。

> **别用「解析到 172.245.211.211」来判断 DNS 是否就绪**：若走 Cloudflare 代理，
> 解析出来是 **Cloudflare 的 IP**，那样判断会误报失败。
> `fix-console-domain-cert.sh` 用的是决定性判据——往 webroot 放一个探测文件，
> 再从公网 `http://<域名>/.well-known/acme-challenge/<file>` 取回来，
> 取到了才继续。两种代理模式都能正确判断。

**排查：nginx 配置写了但访问不了**

按这个顺序查，能一步定位：

```bash
nginx -t                                      # 1. 配置语法
systemctl status nginx --no-pager | head -5   # 2. 服务在跑
ps -eo pid,lstart,args | grep 'nginx: worker' # 3. worker 是否晚于配置修改时间（判断有没有 reload）
getent hosts <域名>                            # 4. DNS 是否存在  ← 最常被忽略
openssl x509 -in /etc/letsencrypt/live/<名>/fullchain.pem -noout -text \
  | grep -A1 'Subject Alternative Name'       # 5. 证书 SAN 是否覆盖该域名
```

> **只在服务器上测是不够的**：配置没 reload 时，用
> `curl -H 'Host: <域名>' http://127.0.0.1/` 仍可能因为**别的站点**兜底而返回
> 看似正常的结果。要确认某个 server 块真的生效，得看
> **worker 进程启动时间是否晚于配置文件修改时间**。
>
> 另外注意 `default` 站点若写成 `return 301 https://$host$request_uri`，
> 那么**任何** Host 头都会被 301，会掩盖「目标站点没生效」的事实。
> 本项目里 `default` 是 `try_files ... =404`，所以 301 确实来自目标站点——
> 这个前提成立时，301 才能作为证据。

> 子路径代理（`/cow/`）**不可行**：控制台的 API 调用是绝对路径
> （`fetch('/config')`、`fetch('/api/agents')`、`fetch('/message')`…），
> 挂到子路径下会打到 DSA 上，且 `/api/*` 会与 DSA 的 `/api/v1/*` 冲突。

**微信登录**：控制台 → Channels → Connect Channel → WeChat → 扫码。
或用 `bash /root/cowagent/show-wechat-qr.sh` 把当前二维码渲染成 PNG。

⚠️ **二维码不是无限自动刷新的**（实测 `channel/weixin/weixin_channel.py`）：

```
QR_LOGIN_TIMEOUT_S = 480     # 整个登录窗口 8 分钟
QR_MAX_REFRESHES   = 10      # 最多刷新 10 次
```

二维码本身约 2 分钟过期、会自动换新，但**刷新满 10 次或总时长到 480s 后通道放弃**
（日志：`QR login timed out` / `请通过控制台重新接入`），此时**必须重启容器**才重开窗口。

`show-wechat-qr.sh` 已处理这一点：它会检测窗口是否已死，死了就自动
`docker restart cowagent` 再等新码；已登录则直接提示无需扫码。
**所以不要提前截图存着，要扫的时候现跑。**

> 脚本里有个容易写错的点：`docker restart` **不会清空日志**，旧二维码链接还在
> `docker logs` 里。若只判断「日志里有没有二维码链接」，重启后会立刻匹配到**旧链接**、
> 渲染出已过期的码。必须比对**链接条数是否增加**。

登录成功后凭据写入 `cow-data/weixin_credentials.json`。
会话过期（errcode `-14`）会自动清凭据并重新发起扫码，无需人工干预。

---

## 9. 已知遗留

- 生产库 21 张表上的 `owner_id` 列（overlay 遗留），当前无害，未清理。
- `dsa_users_legacy_overlay` / `dsa_user_settings_legacy_overlay` / `dsa_invites_legacy_overlay`
  三张归档表，数据保留未删（`dsa_users` 原有 1 行 overlay 调试账号）。
- `china-routes.service` 显示 `inactive (dead)` 但路由已装载且 `enabled` 开机自启；
  `RemainAfterExit=yes` 下出现该状态略反常，因机制工作正常故未改动。
- 旧的 overlay 沙箱容器 `dsa-sandbox` 已停止并置 `--restart=no`，未删除。
