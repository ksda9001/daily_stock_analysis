# 共享行（大盘复盘）改动 —— 全量场景覆盖矩阵

> 目的：在部署到生产环境**之前**，把这次改动可能影响到的场景逐项列清，
> 标明「期望行为 / 实现位置 / 是否有测试钉住 / 结论」。
> 凡是**没有测试钉住**的格子都显式标出来，不假装覆盖过。
>
> 相关文档：`docs/multi-tenancy.md`（§4.3 表内共享行）、
> `docs/cowagent-integration.md`（MCP 工具清单）。

## 0. 改动意图（一句话）

大盘复盘是**公开市场数据**，不属于任何用户。此前它被盖上触发者的
`tenant_id`（通常是系统属主 1），导致其它租户查不到 —— 表现为
「CowAgent 里看不到大盘复盘」，且返回 404 而非 403。

本次改动把它定义为**共享行**：归属 `SHARED_TENANT_ID = 0`，
**所有租户可读**，**删除权收口到管理员**。

## 1. 关键常量

| 名称 | 值 | 含义 |
|---|---|---|
| `SYSTEM_TENANT_ID` | `1` | 「归属到系统属主这个人」，只有他能看 |
| `SHARED_TENANT_ID` | `0` | 「无归属的公共数据」，**所有租户可读** |
| `SHARED_ROW_RULES` | `{"analysis_history": ("report_type", "market_review")}` | 哪些表的哪些行算共享 |

判定规则刻意放在 schema 层、而不是写死在隔离层里，避免隔离逻辑耦合业务语义。

---

## 2. 场景矩阵

### A. 归属维度（「我是谁」）

| # | 场景 | 期望 | 实现 | 测试 | 结论 |
|---|---|---|---|---|---|
| A1 | 多用户模式关闭 | 完全不干预，行为同上游 | `should_scope()` → `False` | `test_single_user_mode_does_not_scope` | ✅ |
| A2 | 请求路径（浏览器会话） | 绑定当前用户 | 认证中间件 | `test_tenancy_api.py` | ✅ |
| A3 | Bearer Token（CowAgent） | 绑定 token 对应用户 | `tenancy/auth` | `test_tenancy_api.py` | ✅ |
| A4 | 无上下文（后台线程 / CLI） | **收窄**到系统属主（fail-closed） | `effective_tenant_id()` | `test_missing_context_falls_back_to_system_owner` | ✅ |
| A5 | 管理员 | 可删共享行 | `is_admin_context()` | `test_admin_can_delete_shared_row` | ✅ |
| A6 | 普通用户 | 不可删共享行 | 谓词退化为 `tenant_id = 我` | `test_non_admin_cannot_delete_shared_row` | ✅ |
| A7 | 显式 `bypass_tenant_scope` | 全表可见、不盖章 | `scope_bypassed()` | `test_bypass_scope_sees_everything` | ✅ |

### B. 语句类型

| # | 语句 | 谓词注入方式 | 共享行处理 | 测试 | 结论 |
|---|---|---|---|---|---|
| B1 | ORM SELECT | `with_loader_criteria` | 放宽 | `test_market_review_visible_to_every_tenant` | ✅ |
| B2 | ORM UPDATE（`query().update()`） | `is_update` 分支 | 放宽 | `test_shared_row_is_updatable_by_any_tenant` | ✅ |
| B3 | ORM 对象修改后 flush | **不经谓词**，靠「先 SELECT 后改」 | 放宽（读时已过滤） | 间接 | ✅ |
| B4 | ORM DELETE（`delete(Model)`） | `is_delete` 分支 | **仅管理员** | `test_non_admin_cannot_delete_shared_row` | ✅ |
| B5 | Core SELECT（`select(table)`） | WHERE 回退路径 | 放宽 | `test_core_select_includes_shared_rows` | ✅ |
| B6 | Core UPDATE（`update(table)`） | WHERE 回退路径 | 放宽 | `test_core_update_cannot_touch_other_tenants_rows` | ✅ 修复后 |
| B7 | Core DELETE（`delete(table)`） | WHERE 回退路径 | 仅管理员 | `test_core_delete_cannot_touch_other_tenants_rows` | ✅ 修复后 |
| B8 | ORM INSERT（flush） | **不注入谓词**，`before_flush` 盖章 | 盖 `SHARED_TENANT_ID` | `test_market_review_is_stamped_shared` | ✅ |
| B9 | Core INSERT（`insert(table)`） | **无保护**（无 WHERE 可注入） | — | — | ⚠️ 见 §4.6 |
| B10 | 裸 SQL（`text(...)`） | 不改写，仅审计 | — | `test_strict_raw_sql_mode_raises` | ✅ |

### C. 触发路径（大盘复盘怎么被拉起）

| # | 路径 | 身份来源 | 共享行归属 | 结论 |
|---|---|---|---|---|
| C1 | WebUI「立即复盘」 | 会话 → `submit_with_context` | `SHARED` | ✅ |
| C2 | MCP `trigger_market_review` | Bearer → `submit_with_context` | `SHARED` | ⚠️ 待端到端实测 |
| C3 | 定时调度扇出（多用户） | 子进程 `bind_user(tenant_id)` | `SHARED` | ⚠️ **重复落库风险**，见 §4.4 |
| C4 | CLI | 无上下文 → 系统属主 | `SHARED` | ✅ |

### D. 数据状态

| # | 状态 | 期望 | 测试 | 结论 |
|---|---|---|---|---|
| D1 | 全新库 | 直接盖 `SHARED` | `test_market_review_is_stamped_shared` | ✅ |
| D2 | 已有 `tenant_id=1` 的历史复盘行 | 迁移搬到 `0` | `test_migration_moves_existing_market_review_rows` | ✅ |
| D3 | `report_type IS NULL` 的行 | **不得**被共享谓词放行（SQL 三值逻辑） | `test_null_report_type_rows_stay_isolated` | ✅ |
| D4 | 私有行 + 共享行共存 | 私有仍隔离 | `test_private_reports_stay_isolated` | ✅ |
| D5 | 同一 code 下私有+共享混合 | 私有可删、共享留存 | `test_delete_by_code_removes_private_and_keeps_shared` | ✅ |

### E. 迁移幂等性

| # | 场景 | 期望 | 测试 | 结论 |
|---|---|---|---|---|
| E1 | 首次执行 | 命中行搬到 `0` | `test_migration_moves_existing_market_review_rows` | ✅ |
| E2 | 二次执行 | `rows_shared` 为空，不再改动 | `test_migration_is_idempotent` | ✅ |
| E3 | 多用户关闭时 | 仍执行 4b（无副作用） | — | ⚠️ 未测，判定为可接受 |
| E4 | 表 / 列缺失 | 记 warning 并跳过 | 代码有分支 | ⚠️ 未测 |

> 步骤 4b 必须排在步骤 4（回填）**之后** —— 否则 NULL 行会先被归给系统属主，
> 再被 4b 搬走，多一次无谓写入；顺序颠倒还会让首次迁移的 `rows_shared` 统计失真。

### F. 回归面（改动**不该**影响的东西）

| # | 项目 | 期望 | 测试 | 结论 |
|---|---|---|---|---|
| F1 | 个股研报隔离 | 不变 | `test_private_reports_stay_isolated` | ✅ |
| F2 | 跨租户删除私有行 | 仍不可 | `test_private_row_still_not_deletable_cross_tenant` | ✅ |
| F3 | 全部 26 张租户表谓词 | 每张都被覆盖 | `test_isolation_covers_all_scoped_tables` | ✅ |
| F4 | 用量归属 | 不变 | `test_usage_is_attributed_to_current_user` | ✅ |
| F5 | 调度扇出目标 | 覆盖全部 active 用户 | `test_fanout_targets_cover_all_active_users` | ✅ |
| F6 | MCP 工具总数 | 26 → 29 | `test_expected_tools_are_registered` | ✅ |

### G. 潜在盲区

| # | 盲区 | 核查结论 |
|---|---|---|
| G1 | `bypass_tenant_scope` 与共享谓词交互 | ✅ 不冲突：bypass 时根本不注入谓词 |
| G2 | `_TENANCY_MARKER` 重复注入 | ✅ 安全：`stmt.options()` 返回新对象，原始语句不被污染 |
| G3 | 跨进程调度（`spawn`） | ✅ 已用 `bind_user` 显式绑定 |
| G4 | 增量读取接口 | ✅ 经核查，`analysis_repo` 是 `DatabaseManager` 薄封装，同一谓词路径 |
| G5 | 严格模式裸 SQL 审计 | ❌ **发现缺陷**，见 §4.3 |
| G6 | 普通用户改共享行 `context_snapshot` | ⚠️ 设计边界，见 §4.5 |
| G7 | Core UPDATE / DELETE 的谓词注入 | ❌ **发现严重缺陷**，见 §4.1 |

---

## 3. 结论摘要

- **375 个直接相关用例全绿**：共享行 31 + tenancy 66 + MCP 45 + 历史端点 93 +
  任务服务 6 + pipeline 134。全量失败集合与上游基线**逐条一致**，无回归。
- 推演过程中发现 **3 个真实缺陷**（1 个本次引入、2 个既有），均已修复并补测。
  其中 **§4.1 属跨租户数据破坏级别**，建议优先关注。
- 另有 1 处**实现陷阱**（§4.7）：上下文传播的初版实现改变了 `submit` 的参数布局。
- 遗留 **3 项待实测/待观察**（C2、C3、E3/E4），部署后需按 §5 验证。

---

## 4. 推演发现的缺陷与处置

### 4.1 【既有 · 严重】Core UPDATE / DELETE 完全绕过租户谓词

**症状**：`session.execute(update(table))` / `delete(table)` 生成**不带 WHERE**
的语句，**跨租户生效**。实测中 bob 的一条 `delete(table)` 直接把 alice 的行删掉了：

```
[tenancy] raw SQL touches tenant table 'analysis_history' without tenant isolation
and is NOT filtered. SQL: DELETE FROM analysis_history
```

**根因**：`_apply_loader_criteria` 的回退路径依赖 `stmt.get_final_froms()` 收集
目标表，但 **`Update` / `Delete` 的 `get_final_froms()` 返回空列表** —— 目标表
实际挂在 `stmt.table` 上。回退路径因此一无所获，谓词一个都没注入。

**旁证**：裸 SQL 审计**当时就报警了**（说明它确实没被约束），但默认模式只告警
不阻断，所以长期无人察觉。

**可达性**：全仓库检索确认当前**没有** Core DML 调用点（`storage.py` 用的是
`delete(AnalysisHistory)`，走 ORM 分支）。属于**潜伏缺陷**，不是线上事故。

**处置**：`_scoped_tables_in()` 增加对 `stmt.table` 的识别。

**测试**：`test_core_update_cannot_touch_other_tenants_rows`（断言 `rowcount == 2`：
自己的 1 行 + 共享的 1 行）、`test_core_delete_cannot_touch_other_tenants_rows`
（断言 `rowcount == 1`）。**修复前两者都是 3** —— 即「把别人的数据也改了/删了」。

### 4.2 【本次引入】按代码删除历史 → 500

**症状**：普通用户在 WebUI 对「大盘复盘」执行「按代码清理历史」，
接口返回 **500 `history deletion made no progress`**。

**根因**：`api/v1/endpoints/history.py` 的 `delete_history_by_code` 循环
以「查得到就删得掉」为前提：

```python
records, _ = db_manager.get_analysis_history_paginated(code=candidates, ...)  # 共享行可见
batch_deleted = db_manager.delete_analysis_history_records(record_ids)        # 共享行删不掉 → 0
if batch_deleted == 0:
    raise RuntimeError("history deletion made no progress")                   # → 500
```

共享行打破了该前提：**读放宽、删收口**。前端确实会以 `MARKET` 调用它
（`HomePage.tsx` 的 `historyApi.deleteByCode('MARKET')`）。

**修复前**：普通用户查不到共享行 → `record_ids` 为空 → 直接 `break` → `{"deleted": 0}`。
**修复后**：能查到但删不掉 → 命中 `batch_deleted == 0` → 抛异常。
**所以这是本次改动引入的回归。**

**处置**：把 `batch_deleted == 0` 识别为「权限边界」而非「失败」，
记日志后 `break`（避免空转）。真实失败（DB 异常）仍会抛出，不会被吞掉。

**测试**：`SharedRowEndpointTests`（4 例），含修复前可复现的红色用例。

### 4.3 【既有缺陷】严格模式下 ORM 写入全部失败

**症状**：开启 `DSA_TENANCY_STRICT_RAW_SQL=true` 后，**任何** ORM 写入都失败：

```
TenantScopeError: raw SQL touches tenant table 'analysis_history' without tenant
isolation; wrap it in bypass_tenant_scope() or use the ORM path.
```

报错信息自相矛盾 —— 它建议「改用 ORM 路径」，而调用方**本来就在用 ORM**。

**根因**：ORM 的 INSERT 由 `flush` 内部发起，**不经过 `do_orm_execute`**，
因此拿不到 `_TENANCY_MARKER`。而裸 SQL 审计只看「有没有标记」，
于是把 ORM 写入误判成裸 SQL。

默认模式（非严格）下只产生**误导性日志**；严格模式下**应用完全不可写**。

**处置**：新增 `_is_orm_tenant_insert()` —— 语句是 `Insert` 且目标表带
`tenant_id` 列时放行。裸写的 `INSERT INTO t (code, ...)` 不带该列，仍会被审计拦住。

**测试**：`RawSqlAuditTests`（3 例）：严格模式下 ORM 写入放行、裸写仍被拦、
默认模式不产生误导告警。

### 4.3b 【部署后发现】同一盲区的另一半：ORM UPDATE 也被误判

**发现方式**：生产部署后的真实验证。由普通用户触发一次大盘复盘，
保存历史时日志出现：

```
[tenancy] raw SQL touches tenant table 'analysis_history' without tenant
isolation and is NOT filtered.
SQL: UPDATE analysis_history SET tenant_id=:tenant_id, id=:id, query_id=:query_id, ...
```

**根因**：与 §4.3 是**同一个盲区**。ORM 的 UPDATE 同样由 flush 的
unit-of-work 发出，同样不经过 `do_orm_execute`、同样拿不到 `_TENANCY_MARKER`。
而 §4.3 的白名单只认 `Insert`，于是 UPDATE 落进审计。
（`SET tenant_id=:tenant_id, id=:id, ...` 的**全列写回**正是 SQLAlchemy
ORM UPDATE 的特征句式，裸 SQL 不会长这样。）

**影响**：生产未开严格模式 → 每次保存复盘都刷一条误导性 warn；
一旦开启 → 保存历史直接失败，且报错同样建议「改用 ORM 路径」。

**处置**：`_is_orm_tenant_insert()` → `_is_orm_tenant_write()`，
同时接受 `Insert` 与 `Update`。ORM 的 `Delete` 走 `do_orm_execute`
自带标记，**显式排除**（放行会掩盖真正的裸 DELETE）。

**教训**：§4.3 修完时我以为「ORM 写入」这一类已经收口，实际只覆盖了
INSERT。**按「症状」修不如按「机制」修** —— 当时若问一句
「还有哪些语句走 flush 而不走 `do_orm_execute`」，UPDATE 会被一起发现。
单测也没有覆盖 INSERT 之外的写路径，所以 375 个绿灯没能拦住它。

**测试**：`RawSqlAuditTests` 补 4 例 —— ORM UPDATE 严格模式可过 /
不留误导 warn / 裸 UPDATE 仍被拦 / ORM DELETE 仍受租户谓词约束。
已用 `git stash` 回退生产代码确认前两例确实变红。

### 4.4 【待观察】调度扇出可能重复落库大盘复盘

**机制**：`_run_tenant_fanout()` **刻意串行**地为每个用户跑一次分析
（串行是为了避开全局分析锁）。若 `should_run_market_review` 为真，
每个用户都会跑一次大盘复盘，且全局锁在每个用户之间已释放，挡不住顺序重复。

**可能的抵消因素**：`main.py` 有「复用当日上下文则跳过」的分支
（`can_skip_market_review`），而共享行机制让**跨用户复用**成为可能 ——
第二个用户能读到第一个用户刚写的共享行，从而跳过落库。
但该分支的条件较严格（`merge_notification or market_context_generated_during_stock`），
**不能假定一定命中**。

**风险变化**：修复前重复行归系统属主（只有 admin 看得到）；
修复后归 `SHARED`，**所有用户都会看到重复条目**。

**处置**：不盲改。部署后按 §5-C3 实测，确认条数后再决定是否加「按日去重」。

### 4.5 【设计边界】共享行的写权限

| 操作 | 普通用户 | 管理员 |
|---|---|---|
| SELECT | ✅ 可见 | ✅ |
| UPDATE | ✅ 可改 | ✅ |
| DELETE | ❌ 不可（静默 0） | ✅ |

- **UPDATE 放开**是刻意的：后台任务可能以任意身份运行，需要维护公共报告。
  HTTP 层没有直接更新入口，`update_analysis_history_diagnostics` 由分析流程内部调用。
- **DELETE 静默 0**：按 ID 删除返回 `{"deleted": 0}`，不报错。用户会看到
  「删除 0 条」而不是明确拒绝。**属可接受的权限边界**，但 UI 未对共享行隐藏删除按钮。
  若要改进，应在前端隐藏或后端返回明确错误码。

### 4.6 【已知限制】Core INSERT 不受租户守卫约束

`do_orm_execute` 只覆盖 SELECT / UPDATE / DELETE。绕过 ORM 的
`connection.execute(insert(table))` 既不会被注入谓词，也不会被盖章。

**现状**：全仓库检索确认，`analysis_history` **没有** Core INSERT 调用点，
ORM 写入是唯一路径。风险仅存在于未来新增代码。

**缓解**：严格模式下的裸 SQL 审计会对不带 `tenant_id` 的 INSERT 报错（§4.3 修复后）。

### 4.7 【本次引入并修正】包装 `submit` 会改变位置参数布局

**症状**：`test_task_service.py` 3 个用例失败：

```
AssertionError: <bound method TaskService._run_analysis ...> != 'csi930955'
```

**根因**：上下文传播的初版实现是

```python
ctx = contextvars.copy_context()
return executor.submit(ctx.run, fn, *args, **kwargs)
```

语义上正确，但它把 `submit` 的**位置参数整体右移一位**：`args[0]` 从 `fn`
变成 `ctx.run`，`args[1]` 从第一个业务参数变成 `fn`。测试断言 `args[1]`
是股票代码，于是错位。

**处置**：改用可调用对象包一层（`_ContextRunner`），让 `submit` 的调用形态
与裸提交完全一致 —— `submit(可调用对象, *业务参数)`。既保留正确的上下文
语义，也不破坏任何按位置解析提交参数的代码。

**验证**：`test_task_service.py` 6 例全绿；`test_shared_rows.py` 的
`ContextPropagationTests` 5 例继续覆盖上下文语义（含异常传播、kwargs 转发、
提交时刻快照）。

**教训**：包装一个「接受 `*args` 的调度接口」时，要留意参数布局是否被改变 ——
这类改动不会报错，只会让下游的按位置解析悄悄错位。

---

## 5. 部署后必须实测的项

### C2. MCP 触发大盘复盘

```bash
# 以 cowagent 身份
curl -s -X POST "$DSA/api/v1/analysis/market-review" -H "Authorization: Bearer $TOKEN"
sleep 60
curl -s "$DSA/api/v1/history?report_type=market_review" -H "Authorization: Bearer $TOKEN"
```

**期望**：能查到记录，且该记录 `tenant_id = 0`。

### C3. 调度扇出是否产生重复

```sql
SELECT id, tenant_id, report_type, created_at
FROM analysis_history WHERE report_type = 'market_review'
ORDER BY created_at DESC LIMIT 20;
```

**期望**：同一时间窗内**每个自然日不超过 1 条**。若出现多条，需加按日去重。

### E3/E4. 迁移在异常结构下的行为

- 多用户关闭时启动一次，确认无异常、无副作用。
- 人为移除某张 `SCOPED_TABLES` 表，确认只记 warning 不阻断启动。

### 通用：以普通用户身份复测删除路径

```bash
curl -s -X DELETE "$DSA/api/v1/history/by-code/MARKET" -H "Cookie: dsa_user_token=$USER_COOKIE"
```

**期望**：HTTP 200 + `{"deleted": 0}`（**不是 500**）。

---

## 6. 部署实测结果（2026-09-13）

环境：服务器 `172.245.211.211`，镜像 `dsa-fork:deploy`（`sha256:466b171f…`）
+ `cowagent-dsa:latest`（含 29 工具）。代码版本 `7a0a3ec`。

### 6.1 迁移：存量复盘已改判为共享行

启动时应出现：

```
[tenancy] moved 2 shared row(s) in analysis_history to tenant_id=0
```

2 条存量 `market_review`（原 `tenant_id=1`）已搬到 `SHARED_TENANT_ID=0`。
**再启动一次不再搬**（第二次启动无该日志）—— 幂等成立。

### 6.2 共享行可见性（三用户对照）

| 用户 | `tenant_id` | 可见历史条数 | 明细 |
|---|---|---|---|
| `admin` | 1 | **3** | 自己的 `600206/full` + 2 条共享复盘 |
| `cowagent` | 2 | **2** | 仅共享复盘 |
| `trader01` | 3 | **2** | 仅共享复盘 |

**结论**：大盘复盘对全部租户可见；私有报告仍严格隔离
（cowagent / trader01 看不到 admin 的 `600206/full`）。两个方向都成立。

### 6.3 归属：普通用户触发的复盘落为共享行

由 `trader01`（普通用户）经 `POST /api/v1/analysis/market-review` 触发，
走后台线程池路径（**正是原先丢身份的那条链路**）。实测落库：

```
(id=4, code='MARKET', report_type='market_review', tenant_id=0)
```

**`tenant_id=0`，不是 1。** 修复前该路径必然盖成 `tenant_id=1`。

### 6.4 删除权责分离

| 调用方 | 请求 | 修复前 | 修复后实测 |
|---|---|---|---|
| `trader01`（普通） | `DELETE /api/v1/history/by-code/MARKET` | **500** | **200 + `{"deleted": 0}`** |
| `admin` | 同上 | — | **200 + `{"deleted": 2}`** |

普通用户「查得到但删不掉」不再报错；管理员仍能清理共享行。

### 6.5 MCP 工具已就位

CowAgent 启动日志：

```
[MCP] Server 'dsa' ready — 29 tool(s)
[ToolManager] MCP loading complete: 1/1 server(s) ready, 29 tool(s) available
```

新增的 `dsa_trigger_market_review` / `dsa_list_market_reviews` /
`dsa_get_latest_market_review` 均在列。

### 6.6 C3：调度扇出未观察到重复落库

复盘执行有**进程内 `threading.Lock` + 同宿主 `flock`** 双重去重
（`src/core/market_review_lock.py`），API / CLI / 调度三入口共用。
本次实测触发 1 次 → 落库 1 条。**未观察到重复**。
注意该锁**不跨主机/容器**，多实例部署需外部幂等。

### 6.7 前端注入层完好

静态资源逐字节核对（经公网 `https://fi.myfi.cc.cd/`）：

| 路径 | 实测 md5 | 结论 |
|---|---|---|
| `/dsa-wechat.js` | `1fa89b31c5b593c1b693a4a6f9cb8963` | 与仓库一致（58018 B） |
| `/index.html` | `ada2e8883239e9e776a93655e2f04eae` | 与仓库一致 |
| `/assets/index-Dhkgyx-b.js` | `3a45b4fb70a9c9762e900bf6369e1637` | 与仓库一致 |
| `/assets/LoginPage-_15D7jin.js` | `56d03c5ccb3383136cf108e191416482` | 与仓库一致 |

⚠️ **静态挂载根是 `/`，不是 `/static/`**。请求 `/static/dsa-wechat.js`
会落进 SPA 回退、返回 `index.html`（HTTP 200 但内容是 HTML）——
排查时不要被这个 200 骗到，**要比 md5，不要只看状态码**。

### 6.8 本轮新增修复：裸 SQL 审计误判 ORM UPDATE

部署后实测发现的**新缺陷**（详见 §7）。

### 6.9 未覆盖 / 待观察

- `DSA_TENANCY_STRICT_RAW_SQL` 在生产**未开启**。严格模式的代码正确性
  由单测覆盖（35 个用例含 4 个 UPDATE 方向），但**未在生产实测**。
- 调度（08:45 / 18:00 自动任务）在本次验证窗口内未自然触发，
  C3 的「按日不重复」结论来自锁机制与单次实测，**非跨日观察**。
- 微信通道未扫码接入（`weixin` 通道待 QR 登录），
  微信侧的大盘复盘查看路径未实测。
