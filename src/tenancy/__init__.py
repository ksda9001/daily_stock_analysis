# -*- coding: utf-8 -*-
"""
===================================
多租户（multi-tenancy）核心包
===================================

本包为 fork 版本引入的原生多用户能力，与上游 ``ZhuLinsen/daily_stock_analysis``
的单管理员模型并存：

- 上游 ``src/auth.py`` 仍然负责「管理员是否启用」与管理员密码；
- 本包在其之上引入「用户（tenant）」概念：每个用户拥有独立的分析历史、
  自选股、通知渠道、调度时间与 LLM 用量归属。

设计原则
--------
1. **默认关闭**：``DSA_MULTIUSER_ENABLED`` 未开启时，全部行为与上游一致。
2. **失败要响**：安全相关的引导阶段失败必须抛出，而不是静默降级。
3. **单一执行点**：租户过滤通过 SQLAlchemy 会话级事件统一施加，
   避免逐条查询手工添加 ``tenant_id`` 造成的遗漏。
4. **系统属主**：后台任务（调度器、CLI）没有请求用户，统一归属到
   ``SYSTEM_OWNER_ID``，从而保持上游单用户行为可用。

模块导航
--------
- :mod:`src.tenancy.context`   —— 当前用户上下文（contextvars）
- :mod:`src.tenancy.models`    —— 用户 / 用户设置 / 审计日志 ORM 模型
- :mod:`src.tenancy.passwords` —— 密码哈希与校验
- :mod:`src.tenancy.tokens`    —— 服务端到服务端 Bearer Token
- :mod:`src.tenancy.schema`    —— ``tenant_id`` 列与用户表的结构引导
- :mod:`src.tenancy.scope`     —— 会话级租户隔离执行点
- :mod:`src.tenancy.settings`  —— 按用户解析配置项
- :mod:`src.tenancy.service`   —— 用户与设置的业务逻辑
- :mod:`src.tenancy.api`       —— FastAPI 路由
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "1.0.0"
