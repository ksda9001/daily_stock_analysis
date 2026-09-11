# -*- coding: utf-8 -*-
"""daily_stock_analysis 的 MCP 服务器。

把 DSA 的 REST API 暴露成 MCP 工具，供 CowAgent 等 Agent 框架调用。

模块划分
--------
``client``
    HTTP 传输层：鉴权、错误映射、输出截断。不依赖 MCP SDK。
``tools``
    工具的业务实现，纯函数，接收 ``DSAClient``。不依赖 MCP SDK。
``server``
    MCP 协议层：注册工具、stdio 启动、SDK 版本兼容。

用法见 ``mcp_server/README.md`` 与仓库根目录的 ``docs/cowagent-integration.md``。
"""

__all__ = ["client", "tools", "server"]
