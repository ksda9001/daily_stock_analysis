# -*- coding: utf-8 -*-
"""CowAgent 发送服务。

职责：把一条已经渲染好的推送正文交给 CowAgent，由它投递到用户的微信。

与 :mod:`src.notification_sender.serverchan3_sender` 是**同一形状**的通道：
拿到正文 → 向固定的 HTTP 端点 POST → 返回成功与否。CowAgent 不做内容加工、
不做定时，只是一个收件口。

这条通道存在的理由
------------------
Server酱3 / 企业微信这类通道只能推到「用户自己配置的那个收件地址」，而这里
的推送目标是**用户在 CowAgent 里绑定的微信**。CowAgent 持有每个收件人的
``context_token``（ilink 发消息的必需凭据，微信侧分配的，DSA 拿不到也给不出），
所以必须由 CowAgent 投递。

分工：
  DSA        —— 决定「给谁推、推什么」并组装正文（本模块只负责交付这一段）
  CowAgent   —— 决定「这条正文怎么落到微信」（分片、重试、token 刷新）
"""

import logging
from typing import Optional

import requests

from src.config import Config

logger = logging.getLogger(__name__)

#: CowAgent 的容器地址。两者同在 dsa-network 上，用服务名即可。
COWAGENT_BASE_URL = "http://cowagent:9899"

#: 投递端点。CowAgent 侧由 channel/dsa_push 注册，只认 Bearer token。
COWAGENT_PUSH_PATH = "/api/dsa-push/send"


class CowAgentSender:
    """把推送正文投递给 CowAgent，由它转发到用户微信。"""

    def __init__(self, config: Config):
        self._base_url = str(
            getattr(config, "cowagent_base_url", None) or COWAGENT_BASE_URL
        ).rstrip("/")
        self._token = str(getattr(config, "cowagent_push_token", None) or "").strip()

    @property
    def configured(self) -> bool:
        """通道是否可用。未配置 token 时不做任何请求 —— 与 Server酱3 未配
        SendKey 就跳过推送的处理一致。"""
        return bool(self._token)

    def send_to_cowagent(
        self,
        content: str,
        receiver: str,
        *,
        timeout_seconds: Optional[float] = None,
    ) -> bool:
        """把 ``content`` 推送给 ``receiver``。

        Args:
            content: 已渲染好的正文（Markdown 原样，CowAgent 不做加工）
            receiver: 收件人微信 ID，形如 ``o9cq...@im.wechat``

        Returns:
            是否成功投递
        """
        receiver = str(receiver or "").strip()
        if not receiver:
            logger.warning("CowAgent 推送缺少收件人，跳过")
            return False
        if not content or not content.strip():
            logger.warning("CowAgent 推送正文为空，跳过")
            return False
        if not self.configured:
            logger.warning("CowAgent 推送 Token 未配置，跳过推送")
            return False

        url = f"{self._base_url}{COWAGENT_PUSH_PATH}"
        headers = {
            "Content-Type": "application/json;charset=utf-8",
            "Authorization": f"Bearer {self._token}",
        }
        payload = {"receiver": receiver, "text": content}

        try:
            response = requests.post(
                url, json=payload, headers=headers, timeout=timeout_seconds or 30
            )
        except Exception as exc:  # noqa: BLE001 - 网络异常不应让调度线程崩掉
            logger.error("推送 CowAgent 失败: %s", exc)
            return False

        if response.status_code != 200:
            logger.error(
                "CowAgent 推送请求失败: HTTP %s, 响应: %s",
                response.status_code,
                (response.text or "")[:300],
            )
            return False

        try:
            result = response.json()
        except ValueError:
            logger.error("CowAgent 推送返回非 JSON: %s", (response.text or "")[:300])
            return False

        if not result.get("ok"):
            # 通道把失败原因放在 error 里（no_context_token / send_failed /
            # no_live_weixin_channel）。原样带出来，便于定位是「用户没跟机器人
            # 说过话」还是「CowAgent 侧没登录」。
            logger.warning(
                "CowAgent 拒绝投递 receiver=%s: %s - %s",
                receiver,
                result.get("error"),
                result.get("detail"),
            )
            return False

        logger.info(
            "CowAgent 投递成功 receiver=%s chars=%s", receiver, result.get("chars")
        )
        return True
