# 多租户（Multi-Tenancy）能力说明

> 本文档描述 **fork 版本**相对上游 [`ZhuLinsen/daily_stock_analysis`](https://github.com/ZhuLinsen/daily_stock_analysis) 新增的多用户能力。
> 上游是「单管理员」模型；本 fork 在其之上叠加了「按用户隔离」的多租户模型，两者可以共存。

---

## 1. 总览

| 维度 | 上游 | 本 fork（多用户模式开启后） |
|---|---|---|
| 身份 | 单一管理员 | 多个用户 + 系统属主 |
| 凭据 | `.admin_password_hash` 文件 | `dsa_users` 表（PBKDF2-SHA256） |
| 会话 | 管理员 Cookie | 用户 Cookie + Bearer Token |
| 数据归属 | 无概念 | 26 张业务表带 `tenant_id` |
| 自选股 | 全局 `.env` 的 `STOCK_LIST` | 每用户一份，可覆盖全局 |
| 通知渠道 | 全局 `.env` | 每用户一份，可覆盖全局 |
| 定时任务 | 全局单任务 | 全局扇出 + 每用户独立时间表 |
| 用量统计 | 全局 | 按用户归属 |

**默认关闭。** 未设置 `DSA_MULTIUSER_ENABLED=true` 时，代码路径与上游完全一致：
不建租户表、不加 `tenant_id` 列、不注册中间件、不改变任何调度行为。

---

## 2. 快速开始

### 2.1 启用

```bash
# .env
DSA_MULTIUSER_ENABLED=true
```

首次启动时 `DatabaseManager` 会自动完成结构引导（幂等）：

1. 创建 `dsa_users` / `dsa_user_settings` / `dsa_tenancy_audit`；
2. 为 26 张业务表补 `tenant_id` 列与索引；
3. 把历史数据回填到系统属主（`tenant_id = 1`）；
4. 播种系统属主用户行（用户名 `admin`，密码复用已有的 `.admin_password_hash`）。

引导完成后才启用租户守卫——**顺序很重要**，否则回填语句自身会被租户谓词限制而漏改历史数据。

### 2.2 管理员如何登录

有两种方式，任选其一：

1. **沿用上游登录**：`POST /api/v1/auth/login` 用管理员密码登录。
   中间件会识别管理员会话并把身份绑定为**系统属主**（`user_id=1`，管理员角色）。
2. **用多用户端点登录**：`POST /api/v1/tenancy/auth/login`，用户名 `admin`，
   密码与上游管理员密码相同（引导时会自动同步）。

### 2.3 创建第一个普通用户

```bash
curl -X POST http://localhost:8000/api/v1/tenancy/users \
  -H "Content-Type: application/json" \
  -H "Cookie: dsa_user_token=<管理员的用户会话>" \
  -d '{"username":"alice","password":"alice-pass-123","role":"user"}'
```

### 2.4 给自动化客户端签发 Token

```bash
curl -X POST http://localhost:8000/api/v1/tenancy/auth/token \
  -H "Content-Type: application/json" \
  -d '{"username":"alice","password":"alice-pass-123"}'
# → {"token":"v1.2.1.1799....","token_type":"Bearer","user":{...}}
```

后续调用带上：

```
Authorization: Bearer v1.2.1.1799....
```

---

## 3. 数据隔离是怎么实现的

### 3.1 为什么不逐条查询加条件

`src/storage.py` 有 4400+ 行、上百个查询方法。逐条添加 `WHERE tenant_id = ?` 需要改动上百处，
而且**新增查询时极易遗漏**——遗漏的表现是「悄悄看到别人的数据」而不是报错，属于最危险的一类缺陷。

因此采用**单一执行点**：在 SQLAlchemy 的 `do_orm_execute` 会话事件上统一施加租户谓词
（`src/tenancy/scope.py`）。任何 ORM 语句（SELECT / UPDATE / DELETE）只要涉及租户表，
就自动被限制在当前用户范围内。

### 3.2 归属判定

```python
def effective_tenant_id() -> int:
    return current_user_id() or SYSTEM_TENANT_ID   # 系统属主 = 1
```

**这是整个隔离体系的唯一归属判定函数。** 多用户模式下上下文缺失时会**收窄**到系统属主，
而不是**放宽**到全部数据——这是刻意的 fail-closed 设计。

唯一的例外是**共享行**（见 §4.3）：那不是「放宽」，而是这类数据本来就不属于任何用户。

### 3.3 写入归属

`before_flush` 事件为所有新增的租户表对象盖上 `tenant_id` 戳。
业务代码不需要显式赋值，也不可能忘记。

命中共享行规则的对象盖 `SHARED_TENANT_ID`（`0`），其余盖当前租户。

### 3.4 逃生舱

跨租户的合法场景（运维统计、数据迁移）必须显式声明：

```python
from src.tenancy.context import bypass_tenant_scope

with bypass_tenant_scope("monthly cross-tenant report"):
    ...  # 这里能看到全部用户的数据
```

调用会打 WARNING 日志，便于审计。

### 3.5 已知边界

| 场景 | 是否被隔离 | 说明 |
|---|---|---|
| ORM 查询（`session.query` / `session.execute(select(...))`） | ✅ | 覆盖业务代码的全部查询路径 |
| ORM 写入 | ✅ | `before_flush` 盖章 |
| 裸 SQL（`connection.execute(text(...))`） | ⚠️ 仅审计 | 默认告警一次；`DSA_TENANCY_STRICT_RAW_SQL=true` 时直接报错 |
| 全局市场数据表 | ➖ 不隔离 | 见 §4.2 |
| 表内共享行（如大盘复盘） | ➖ 读不隔离 / 删仅管理员 | 见 §4.3 |
| 后台线程提交的任务 | ⚠️ 需显式传播上下文 | 见 §3.6 |

裸 SQL 之所以不做改写：可靠地改写任意 SQL 需要完整的 SQL 解析器，
收益不抵复杂度。上游仅在 `DatabaseManager.__init__` 的迁移阶段使用裸 SQL，
且该阶段发生在守卫启用之前。审计钩子的作用是**防止未来新增的裸 SQL 悄悄绕过隔离**。

---

## 4. 表分类

### 4.1 按用户隔离（26 张，带 `tenant_id`）

| 分类 | 表 |
|---|---|
| 分析 | `analysis_history`、`backtest_results`、`backtest_summaries`、`screening_runs` |
| 对话 / Agent | `conversation_messages`、`conversation_session_states`、`conversation_summaries`、`agent_provider_turns` |
| 用量 | `llm_usage` |
| 告警 | `alert_rules`、`alert_triggers`、`alert_notifications`、`alert_cooldowns` |
| 决策信号 | `decision_signals`、`decision_signal_outcomes`、`decision_signal_feedback` |
| 技能观点 | `skill_opinion_samples`、`skill_opinion_outcomes` |
| 持仓 | `portfolio_accounts`、`portfolio_trades`、`portfolio_cash_ledger`、`portfolio_corporate_actions`、`portfolio_positions`、`portfolio_position_lots`、`portfolio_daily_snapshots`、`portfolio_fx_rates` |

### 4.2 全局共享（6 张，**不加** `tenant_id`）

`stock_daily`、`news_intel`、`intelligence_items`、`intelligence_sources`、`fundamental_snapshot`、`schema_migrations`

这是**刻意的设计决定**，不是遗漏：

- 行情与基本面快照对所有用户完全相同，共享可避免 N 倍冗余与 N 倍数据源请求；
- 新闻/情报是公开内容的抓取缓存，不含用户私有信息；
- 共享能显著降低被数据源按 IP 限流的概率。

代价是「用户 A 抓到的新闻用户 B 也能看到」——对公开市场数据而言可接受。

### 4.3 表内共享行（公共数据）

有些表**既装私有行也装公共行**，无法整表归入 §4.2。典型是 `analysis_history`：
它同时装着每个人的个股研报（私有）和每日**大盘复盘**（公共）。

这类行由 `src/tenancy/schema.py` 的 `SHARED_ROW_RULES` 声明：

```python
SHARED_ROW_RULES = {
    "analysis_history": ("report_type", "market_review"),
}
```

命中规则的行归属为 `SHARED_TENANT_ID`（默认 `0`，可用 `DSA_SHARED_TENANT_ID` 覆盖），
并且按语句类型分级放开：

| 语句 | 共享行是否参与匹配 | 理由 |
|---|---|---|
| `SELECT` | ✅ 所有租户可见 | 「共有数据」的定义 |
| `UPDATE` | ✅ 所有租户可改 | 后台任务需维护公共报告；HTTP 层没有更新入口 |
| `DELETE` | ⚠️ **仅管理员** | 否则任一普通用户就能删掉公共报告 |

写入时 `before_flush` 会自动盖 `SHARED_TENANT_ID`，业务代码无需关心。
启动迁移（`ensure_tenancy_schema` 步骤 4b）会把历史遗留的共享行搬到该归属下，幂等。

> **判定式必须只依赖列属性**，因为同一个规则要同时用于 ORM 类（`cls.report_type`）、
> Core `Table`（`table.c["report_type"]`）和 Python 实例（`obj.report_type`）。
> ⚠️ Core `Table` **不代理属性访问**，写成 `table.report_type` 会抛 `AttributeError`。

### 4.4 ⚠️ 命名冲突提醒

上游 `portfolio_accounts` **已有一个** `owner_id = Column(String(64))` 字段，
语义是「账户持有人标签」（自由文本），被 `portfolio_service` / `portfolio_repo` 使用。

**因此租户列命名为 `tenant_id` 而不是 `owner_id`。** 若沿用 `owner_id`，
租户过滤会与这个字符串字段撞名，导致 ORM 属性被覆盖——
这是一个只在持仓功能上暴露、且症状是「持仓数据错乱」的隐蔽缺陷。

修改持仓相关代码时请注意区分这两个字段。

---

## 5. 按用户配置

### 5.1 实现方式：声明式映射 + 配置副本覆盖

上游的配置是**进程级全局**的（`src/config.py` 从 `.env` 读一次，全局共享一个 `Config` 实例）。
多用户要求每个用户有自己的自选股、通知渠道、调度时间。

`src/tenancy/settings.py` 的 `USER_SETTING_SPECS` 声明「哪些环境变量可被用户覆盖」及
其对应的 `Config` 属性名与类型；`apply_user_overrides(config, tenant_id)` 复制一份 `Config`
并写入该用户的值。

这样上游所有读取 `config.<attr>` 的代码（通知发送、报告渲染、Agent 工具……）
**完全不需要修改**就能按用户生效。改动面从「上百处读取点」收敛到「一个复制点」。

调用点：`main.py::run_full_analysis` 开头（API、调度器子进程、CLI 三条入口都经过它），
以及 Agent 问股端点（`api/v1/endpoints/agent.py::_request_config()`）。
两者都收敛到同一个函数 `src/tenancy/settings.py::tenant_config()`。

⚠️ 必须在**请求处理线程**里解析：`contextvars` 不跨线程，`run_in_executor`
出来的 worker 里拿不到当前用户，所以要先解析好再把结果传进去。

### 5.2 可覆盖的配置项

自选股、报告类型、Agent 模式、问股上下文压缩，以及各通知渠道的凭据：

`STOCK_LIST`、`REPORT_TYPE`、`AGENT_MODE`、`AGENT_CONTEXT_COMPRESSION_ENABLED`、
`WECHAT_WEBHOOK_URL`、`DINGTALK_WEBHOOK_URL`、`FEISHU_WEBHOOK_URL`、`FEISHU_WEBHOOK_SECRET`、
`FEISHU_WEBHOOK_KEYWORD`、`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`、`EMAIL_SENDER`、
`EMAIL_PASSWORD`、`EMAIL_RECEIVERS`、`SERVERCHAN3_SENDKEY`、`PUSHPLUS_TOKEN`、
`PUSHOVER_USER_KEY`、`PUSHOVER_API_TOKEN`、`NTFY_URL`、`GOTIFY_URL`、`GOTIFY_TOKEN`、
`DISCORD_WEBHOOK_URL`、`SLACK_WEBHOOK_URL`、`CUSTOM_WEBHOOK_URLS`、`ASTRBOT_URL`

> ⚠️ 调度相关（`SCHEDULE_ENABLED` / `SCHEDULE_TIMES`）**不在**可覆盖之列 ——
> 它们决定整机跑不跑、几点跑，属于基础设施配置，见 §5.3。

### 5.3 明确禁止用户覆盖

`DATABASE_PATH`、`ADMIN_AUTH_ENABLED`、`DSA_MULTIUSER_ENABLED`、`SCHEDULE_ENABLED`、
`SCHEDULE_TIMES`、`SCHEDULE_TIME`、各 LLM 供应商密钥（`OPENAI_API_KEY`、
`DEEPSEEK_API_KEY`、`ANTHROPIC_API_KEY`、`GEMINI_API_KEY`、`QWEN_API_KEY`）、
`LITELLM_CONFIG`、`LLM_CHANNELS`、`DSA_API_TOKEN_TTL_SECONDS`。

这些属于**基础设施配置**：允许用户覆盖等于允许用户越权（改数据库路径、关掉鉴权、盗用 LLM 配额）。
`save_user_settings` 会拒绝写入并记 WARNING；`apply_user_overrides` 也会二次过滤。

### 5.4 敏感项掩码

`GET /api/v1/tenancy/settings` 对 `secret=True` 的项做掩码（保留前 4 后 4 位），
避免 Webhook / Token 明文出现在前端与日志里。

---

### 5.5 用户级系统配置：普通成员也能改的少数几项

问股页的「上下文压缩」开关走的是 `PUT /api/v1/system/config`，而这个端点原本**只对管理员开放**。
普通成员一按就得到 403「普通成员无权访问或修改系统设置」，开关永远存不上。

修法是**开一个极小的白名单**，而不是放开整个端点：

* `src/tenancy/settings.py::USER_SCOPED_CONFIG_KEYS` 声明哪些键允许普通成员自行调整；
* **读取**：`GET /api/v1/system/config` 对普通成员**按白名单重建**响应体，只含这几项，
  值是**生效值**（用户值 → 全局值），`llm_model_providers` 置空；
* **写入**：`PUT /api/v1/system/config` 对普通成员**只接受白名单内的键**，且写入的是
  `dsa_user_settings`，**不碰 `.env`**；因此不做全局版本校验、不触发运行时重载；
* **版本号**：普通成员拿到 `user-scope:<sha256 前 16 位>`，只对这几项做指纹 ——
  全局 `config_version` 是整个 `.env` 的 `mtime:sha256`，给出去等于泄露全部密钥的哈希。

三条必须守住的边界（单测 `tests/test_system_config_user_scope.py`，14 例）：

1. 普通成员的响应里**不得出现**任何非白名单键（LLM 密钥、Webhook、`DATABASE_PATH`……）；
2. 混合请求**整批拒绝**（合法项也不能部分生效），避免「探测式」写入；
3. 写入**不改动** `.env`，也不改动全局运行时 `Config`。

### 5.6 系统设置端点的角色边界

多用户模式下 `/api/v1/system/config*` 的角色要求：

| 端点 | 要求 |
|---|---|
| `GET /config` | 管理员见全量；普通成员见白名单（§5.5） |
| `PUT /config` | 管理员写 `.env`；普通成员写自己的 `dsa_user_settings`（§5.5） |
| `GET /config/export`、`POST /config/import` | 管理员会话（`_allow_env_backup_access`） |
| `POST /config/llm/test-channel`、`/config/llm/discover-models`、`/config/notification/test-channel`、`/config/generation-backends/smoke-test` | **必须管理员** |
| `GET /config/schema`、`/config/setup/status`、各类 `status` / `preview` | 不额外限制（只读、无密钥外泄、不产生调用成本） |

后一档里的四个端点原本**没有任何角色检查**，而它们的 `use_saved_secret=True`
会让服务端拿**已保存的**密钥去发请求 —— 普通成员若能调用，等于借用管理员的
LLM 额度与推送通道。2026-09-14 已补齐 `_assert_admin()`。

---

## 6. 按用户调度

### 6.1 上游的调度链路

```
Scheduler (schedule 库, 30s 轮询)
  └─ RuntimeSchedulerService._start_analysis_watchdog
       └─ multiprocessing spawn 子进程
            └─ _run_scheduled_analysis_process
                 └─ RuntimeSchedulerService._run_analysis_locked
                      └─ main.run_scheduled_analysis → run_full_analysis
```

### 6.2 多用户下的三个关键改动

**（1）子进程必须显式绑定身份。**
`spawn` 子进程**不继承**父进程的 contextvars。`tenant_id` 通过进程参数传入，
子进程内用 `bind_user()` 绑定。不这么做的话，该用户的分析会退化为系统属主身份，
读到管理员的配置并把结果写进管理员的账户。

**（2）全局任务从「跑一次」改为「扇出」。**
多用户模式下，全局 `SCHEDULE_TIMES` 触发时不再是「以系统属主身份跑一次」，
而是**为每个跟随全局时间的用户各跑一次**（`_start_tenant_fanout`）。

**（3）用户可拥有个人时间表。**
用户在 `dsa_user_settings` 里配置 `SCHEDULE_TIMES` 后，
调度器通过新增的 `Scheduler.add_daily_task()` 注册独立的具名每日任务（`tenant-<id>`）。

### 6.3 用户归属规则

| 用户的配置 | 行为 |
|---|---|
| 未设置任何调度项 | 跟随全局时间，被扇出执行 |
| `SCHEDULE_ENABLED=true` + `SCHEDULE_TIMES` | 独立任务，**不**参与扇出（避免重复） |
| `SCHEDULE_ENABLED=false` | 完全不执行 |
| `SCHEDULE_TIMES` 已设置但 `SCHEDULE_ENABLED` 未设置 | 不执行（必须显式开启） |

### 6.4 并发模型

扇出是**串行**的，因为全局分析锁 `_RUNTIME_ANALYSIS_LOCK` 会串行化任何并发分析；
并行扇出只会让后到的用户被记为 `analysis_already_run` 而跳过。

串行的副作用是 N 个用户的定时分析总耗时约为 N 倍。这是**刻意的权衡**：
并行执行会同时向 AkShare / Baostock / YFinance 发起 N 倍请求，
在按 IP 限流的数据源上极易触发封禁——这是「一个容器多用户」方案的主要风险，
串行调度正是为了规避它。用户量增长后若需要并行，应优先增加数据源配额或引入请求队列，
而不是简单放开并发。

---

## 7. API 一览

前缀：`/api/v1/tenancy`

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/capabilities` | 公开 | 探测是否启用多用户、支持的配置键 |
| GET | `/health` | 公开 | 子系统健康检查 |
| POST | `/auth/login` | 公开 | 用户名密码登录，写入 Cookie |
| POST | `/auth/token` | 公开 | 签发 Bearer Token（自动化客户端用） |
| POST | `/auth/logout` | 任意 | 清除 Cookie |
| GET | `/auth/me` | 已登录 | 当前用户信息 |
| POST | `/auth/change-password` | 已登录 | 修改自己的密码（吊销全部 Token） |
| GET | `/users` | 管理员 | 用户列表 |
| POST | `/users` | 管理员 | 创建用户 |
| PATCH | `/users/{id}` | 管理员 | 改显示名 / 角色 / 状态 |
| DELETE | `/users/{id}` | 管理员 | 删除用户（系统属主受保护） |
| POST | `/users/{id}/reset-password` | 管理员 | 重置密码 |
| POST | `/users/{id}/token` | 管理员 | 代签发 Token |
| POST | `/users/{id}/revoke-tokens` | 管理员 | 吊销该用户全部 Token |
| GET | `/settings` | 已登录 | 读自己的配置（敏感项掩码） |
| PUT | `/settings` | 已登录 | 写自己的配置 |
| DELETE | `/settings` | 已登录 | 重置部分配置，回落到全局 `.env` |
| GET | `/watchlist` | 已登录 | 读自己的自选股（首次读取继承全局 `STOCK_LIST`） |
| POST | `/watchlist` | 已登录 | 加入一只股票（**在继承列表上追加**，不是替换） |
| PUT | `/watchlist` | 已登录 | 整体替换自己的自选股 |
| DELETE | `/watchlist` | 已登录 | 清空个人自选，回落到全局 `STOCK_LIST` |
| DELETE | `/watchlist/{stock_code}` | 已登录 | 从自选移除一只股票 |
| GET | `/usage` | 已登录 | 自己的 LLM 用量 |
| GET | `/usage/{id}` | 管理员 | 指定用户用量 |
| GET | `/scheduler/users` | 管理员 | 参与定时分析的用户及其调度配置 |
| GET | `/audit` | 管理员 | 租户操作审计日志 |

> **为什么自选股必须走 `/api/v1/tenancy/watchlist` 而不是上游的
> `/api/v1/stocks/watchlist/add`？**
>
> 上游那个接口最终调用 `SystemConfigService` 写入**进程级全局** `STOCK_LIST`，
> 也就是所有用户共享同一份。多用户场景下 A 加一只股票，B 的自选会跟着变，
> 而且下一次分析 B 会莫名其妙多分析一只股票。租户端点的写入落在
> `dsa_user_settings` 里按 `tenant_id` 隔离，不会互相污染。
>
> 因此 `mcp_server` 的自选股工具**全部**指向 `/api/v1/tenancy/watchlist`，
> 并在 `tests/test_mcp_server.py` 里有一条断言专门守住这一点：
> 一旦有人把它改回上游路径，测试立即失败。

---

## 8. 安全说明

### 8.1 Token 是无状态签名凭证

格式：`v1.<user_id>.<token_version>.<expires_at>.<nonce>.<hmac_sha256>`

- 签名密钥独立存放于 `DATA_DIR/.api_token_secret`（与浏览器会话密钥分离，可单独轮换）；
- **不携带角色**——角色每次请求从数据库读取，因此「签发后降权」会立即生效；
- `token_version` 递增即吊销该用户全部 Token（改密码、重置密码、停用、显式吊销都会递增）。

**Token 等同于该用户的密码，必须通过 HTTPS 传输。**

### 8.2 防止用户名枚举

`authenticate()` 在用户不存在时也会执行一次哈希校验（对空摘要），
避免通过响应时间区分「用户不存在」与「密码错误」。

### 8.3 审计

用户增删改、密码变更、Token 签发/吊销均写入 `dsa_tenancy_audit`，
记录操作者、目标、动作、IP 与时间。

### 8.4 删除用户的语义

删除用户**不会**级联删除其业务数据（分析历史、持仓……）。
那属于不可逆的数据销毁，必须由运维显式执行。删除只清理用户行与设置；
其余数据成为孤儿记录（`tenant_id` 指向已删除用户），**不会泄漏给其他用户**
（因为租户谓词要求 `tenant_id == 当前用户`，孤儿数据匹配不到任何活跃用户）。

---

## 9. 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `DSA_MULTIUSER_ENABLED` | `false` | 多用户模式总开关 |
| `DSA_SYSTEM_TENANT_ID` | `1` | 系统属主用户 ID |
| `DSA_API_TOKEN_TTL_SECONDS` | `2592000`（30 天） | Bearer Token 有效期，钳制在 5 分钟 ~ 365 天 |
| `DSA_TENANCY_STRICT_RAW_SQL` | `false` | `true` 时裸 SQL 触碰租户表直接报错 |
| `DSA_TENANCY_AUDIT_LOG` | — | 保留，用于后续接入更细粒度审计 |

---

## 10. 与上游同步

本 fork 的改动集中在：

```
src/tenancy/                        # 新增，完全自包含
src/storage.py                      # +26 处 tenant_id 列 +bootstrap 调用
src/scheduler.py                    # +add_daily_task / cancel_named_daily_task
src/services/runtime_scheduler.py   # per-user 调度与扇出
src/config.py                       # refresh_stock_list 支持用户级自选股
main.py                             # _apply_tenant_config + run_full_analysis 挂钩
api/app.py                          # 安装多租户
api/middlewares/auth.py             # 多用户模式下让行
tests/test_tenancy.py               # 新增测试
docs/multi-tenancy.md               # 本文档
```

同步上游的推荐流程：

```bash
git remote add upstream https://github.com/ZhuLinsen/daily_stock_analysis.git
git fetch upstream
git merge upstream/main        # 或 git rebase upstream/main
```

冲突最可能出现在 `src/services/runtime_scheduler.py`（上游改动频繁）与 `src/storage.py`。
`src/tenancy/` 与上游无重叠，基本不会冲突。

合并后务必重跑 `pytest tests/test_tenancy.py`——
特别是 `test_tenant_column_added_to_all_scoped_tables`：
**上游新增业务表时，需要手动把表名加入 `src/tenancy/schema.py` 的 `SCOPED_TABLES`**，
否则新表不受租户隔离。
