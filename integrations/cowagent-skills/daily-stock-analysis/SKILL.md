---
name: daily-stock-analysis
display_name: DSA 智能量化投研
description: A股/美股/港股实时行情、自选股管理、筹码分布(CYQ)计算、大盘复盘、多专家投研报告与多因子策略选股。
category: finance
---

# DSA 智能量化投研专家系统 (Daily Stock Analysis)

当用户询问股票行情、自选股维护、战法打分、量化选股、大盘复盘或深度研报时，通过 MCP 协议自动调用本技能关联的 29 个量化分析工具。

## 🎯 核心能力与工具映射

### 1. 实时行情与基本面
- **查询个股秒级盘口**：自动调用 `dsa_get_stock_quote(stock_code)` 获取最新价、涨跌幅、成交量与换手率。
- **上市公司基本面画像**：自动调用 `dsa_get_stock_profile(stock_code)` 获取所属行业、市盈率 PE、市净率 PB 与主营构成。

### 2. 自选股云端管理（按用户隔离）
- **查看我的自选**：自动调用 `dsa_get_watchlist()` 获取当前绑定的股票池。
- **添加股票进自选**：自动调用 `dsa_add_to_watchlist(stock_code)`（不覆盖全局，个人专属）。
- **从自选删除股票**：自动调用 `dsa_remove_from_watchlist(stock_code)`。
- **批量同步自选股**：自动调用 `dsa_replace_watchlist(stock_codes)`。

### 3. 大盘复盘（公共数据，所有用户共享）
大盘复盘是**全局共有数据**——不区分用户，任何人产生的复盘所有用户都能看到。
- **触发大盘复盘**：调用 `dsa_trigger_market_review(send_notification)` 提交后台任务，返回 `task_id`。
  大盘复盘耗时较长（需抓取全市场行情与板块数据，通常 1–3 分钟），提交后应告知用户稍候。
- **查看历史复盘列表**：调用 `dsa_list_market_reviews()` 获取最近的大盘复盘记录。
- **读取最新复盘**：调用 `dsa_get_latest_market_review()` 直接拿到最新一份复盘正文。
  无记录时返回 `{"found": false}`，属正常情况，应如实告知用户「暂无复盘记录」并建议触发一次。

### 4. 多专家投研深度分析
- **发起个股/全自选分析**：调用 `dsa_analyze_stocks(stock_codes, report_type="detailed")`，启动包含技术分析、筹码CYQ、新闻舆情与风控仲裁的完整决策流水线。
- **异步进度与结果跟踪**：调用 `dsa_get_analysis_task(task_id)` 查询任务状态。
- **调取结构化研报**：调用 `dsa_get_analysis_report(record_id)` 或 `dsa_get_report_markdown(record_id)`。

### 5. 量化选股与题材挖掘
- **全市场战法选股**：调用 `dsa_run_screening(strategy)` 运行双低、多因子、价值成长等策略，筛选符合买点的股票。
- **题材热点监控**：调用 `dsa_get_market_hotspots()` 扫描盘面领涨题材与资金流向。

### 6. 决策信号与问股
- **买卖评级与风控状态**：调用 `dsa_get_decision_signal(stock_code)` 查询买入/卖出/观望评级及风控一票否决依据。
- **深度研报模式**：调用 `dsa_run_deep_research(question, stock_code)` 联网检索深度资讯与行业研报。

## 💡 典型触发示例
- “帮我查一下 600206 现在的行情走势和最新价格”
- “把 300655 加到我的自选股里”
- “查看我的自选股列表”
- “今天有什么量化选股推荐？用双低策略筛选一下”
- “对 002747 进行深度分析，给出详细投研研报”
- “低空经济板块最近有哪些热点催化？”
- “今天大盘怎么样？帮我做一次大盘复盘”
- “最近的大盘复盘报告给我看看”

## ⚠️ 使用提示
- 大盘复盘为全局共享数据：**任何用户触发的复盘，所有用户都能查看**。用户问“今天大盘怎么样”时，可先 `dsa_get_latest_market_review()` 拿最近一份；若已过期或为空，再触发新的。
- 个股研报、自选股、决策信号是**按用户隔离**的私有数据，不会跨用户可见。
