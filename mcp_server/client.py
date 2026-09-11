# -*- coding: utf-8 -*-
"""DSA REST 客户端。

只依赖 ``httpx``，**不依赖 MCP SDK** —— 这样它既能被单元测试直接驱动，
也能在别的地方（脚本、其他 Agent 框架）复用。

鉴权
----
按以下优先级解析身份：

1. ``DSA_API_TOKEN``：推荐。用 ``POST /api/v1/tenancy/auth/token`` 事先签发；
2. ``DSA_USERNAME`` + ``DSA_PASSWORD``：首次请求时惰性换取 Token。

Token 只保存在内存里，不落盘、不进日志。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:8000"

#: 普通接口超时
DEFAULT_TIMEOUT = 60.0
#: 分析 / 选股这类长任务要等很久
LONG_TIMEOUT = 900.0

#: 单个工具返回给 Agent 的最大字符数。
#: 完整研报动辄几十 KB，不截断会直接撑爆模型上下文 —— 这是 MCP 工具最
#: 常见的翻车点之一。
MAX_TEXT_CHARS = 24000

_TRUNCATION_NOTICE = (
    "\n\n…（输出过长已截断，原始长度 {total} 字符。"
    "需要完整内容请缩小范围，或改用 get_analysis_report 的 max_chars 参数。）"
)


class DSAApiError(RuntimeError):
    """DSA 调用失败：HTTP 非 2xx、鉴权失败或网络异常。"""

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        code: str = "",
        payload: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.payload = payload

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "ok": False,
            "error": self.code or "request_failed",
            "message": self.message,
        }
        if self.status_code is not None:
            out["status_code"] = self.status_code
        if isinstance(self.payload, dict) and self.payload:
            out["detail"] = self.payload
        return out


# ---------------------------------------------------------------------------
# 输出整形
# ---------------------------------------------------------------------------

def truncate(text: Any, limit: int = MAX_TEXT_CHARS) -> str:
    """按字符数截断，并附上说明，避免模型误以为内容就这么多。"""
    if text is None:
        return ""
    text = str(text)
    if limit is None or limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + _TRUNCATION_NOTICE.format(total=len(text))


def to_text(payload: Any, limit: int = MAX_TEXT_CHARS) -> str:
    """把任意返回值转成给模型看的紧凑 JSON 文本。"""
    if isinstance(payload, str):
        return truncate(payload, limit)
    try:
        text = json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(payload)
    return truncate(text, limit)


def _extract_error(resp: httpx.Response, payload: Any):
    """从各种错误体里挖出可读信息。

    上游 FastAPI 用 ``{"detail": ...}``，本仓库的多租户接口用
    ``{"error": ..., "message": ...}`` —— 两种都要认。
    """
    if isinstance(payload, dict):
        message = payload.get("message")
        if isinstance(message, str) and message:
            return message, str(payload.get("error") or "")
        detail = payload.get("detail")
        if isinstance(detail, str) and detail:
            return detail, ""
        if isinstance(detail, dict):
            text = detail.get("message") or detail.get("error") or json.dumps(
                detail, ensure_ascii=False
            )
            return str(text), str(detail.get("error") or "")
        if detail is not None:
            return str(detail), ""
    text = (resp.text or "").strip()
    if text:
        return text[:500], ""
    return f"HTTP {resp.status_code}", ""


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------

class DSAClient:
    """同步 HTTP 客户端。

    :param base_url: DSA 服务地址，默认取 ``DSA_BASE_URL`` 或 ``http://127.0.0.1:8000``
    :param token: Bearer Token，默认取 ``DSA_API_TOKEN``
    :param transport: 仅供测试注入 ``httpx.MockTransport``
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        *,
        username: Optional[str] = None,
        password: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self.base_url = (
            base_url or os.environ.get("DSA_BASE_URL") or DEFAULT_BASE_URL
        ).rstrip("/")
        self._token = token or os.environ.get("DSA_API_TOKEN") or None
        self._username = username or os.environ.get("DSA_USERNAME") or None
        self._password = password or os.environ.get("DSA_PASSWORD") or None
        self.timeout = timeout
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
        )

    # -- 生命周期 ---------------------------------------------------------

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> "DSAClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    @property
    def has_credentials(self) -> bool:
        return bool(self._token or (self._username and self._password))

    # -- 鉴权 -------------------------------------------------------------

    def _login(self) -> str:
        if not (self._username and self._password):
            raise DSAApiError(
                "缺少凭据：请设置 DSA_API_TOKEN，或同时设置 DSA_USERNAME 与 DSA_PASSWORD",
                code="missing_credentials",
            )
        try:
            resp = self._client.post(
                "/api/v1/tenancy/auth/token",
                json={"username": self._username, "password": self._password},
            )
        except httpx.HTTPError as exc:
            raise DSAApiError(
                f"无法连接 DSA（{self.base_url}）: {exc}", code="connection_error"
            ) from exc

        payload: Any = None
        try:
            payload = resp.json()
        except ValueError:
            payload = None

        if resp.status_code >= 400:
            message, code = _extract_error(resp, payload)
            raise DSAApiError(
                message, status_code=resp.status_code, code=code or "login_failed", payload=payload
            )

        token = (payload or {}).get("token") if isinstance(payload, dict) else None
        if not token:
            raise DSAApiError(
                "登录成功但响应里没有 token 字段", code="login_failed", payload=payload
            )
        logger.info("[mcp] 已用 %s 换取 Token", self._username)
        return str(token)

    def _ensure_token(self) -> None:
        if not self._token:
            self._token = self._login()

    # -- 请求 -------------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        """发起一次调用，返回解析后的 JSON（或 ``{"raw": ...}``）。"""
        self._ensure_token()

        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        try:
            resp = self._client.request(
                method,
                path,
                json=json_body,
                params=params,
                headers=headers,
                timeout=timeout or self.timeout,
            )
        except httpx.HTTPError as exc:
            raise DSAApiError(
                f"无法连接 DSA（{self.base_url}）: {exc}", code="connection_error"
            ) from exc

        payload: Any = None
        try:
            payload = resp.json()
        except ValueError:
            payload = None

        if resp.status_code >= 400:
            message, code = _extract_error(resp, payload)
            raise DSAApiError(
                message, status_code=resp.status_code, code=code, payload=payload
            )

        if payload is None:
            return {"raw": (resp.text or "")[:MAX_TEXT_CHARS]}
        return payload

    def get(self, path: str, **kwargs) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs) -> Any:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs) -> Any:
        return self.request("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs) -> Any:
        return self.request("DELETE", path, **kwargs)
