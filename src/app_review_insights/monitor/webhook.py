"""群机器人 Webhook 推送：飞书 / 钉钉 / 企业微信 / Slack。

未配置 Webhook 时调用方应跳过推送（send_report_by_id 返回空列表）。
"""

from __future__ import annotations

from typing import Any

import httpx


class WebhookSender:
    def __init__(self, client: httpx.Client | None = None, timeout_seconds: float = 15):
        self.client = client or httpx.Client(timeout=timeout_seconds)

    # ---- 各渠道 payload 格式 ----
    @staticmethod
    def feishu_payload(text: str) -> dict[str, Any]:
        return {"msg_type": "text", "content": {"text": text}}

    @staticmethod
    def dingtalk_payload(text: str) -> dict[str, Any]:
        return {"msgtype": "text", "text": {"content": text}}

    @staticmethod
    def wecom_payload(text: str) -> dict[str, Any]:
        return {"msgtype": "text", "text": {"content": text}}

    @staticmethod
    def slack_payload(text: str) -> dict[str, Any]:
        return {"text": text}

    @classmethod
    def payload_for(cls, channel: str, text: str) -> dict[str, Any]:
        builders = {
            "feishu": cls.feishu_payload,
            "dingtalk": cls.dingtalk_payload,
            "wecom": cls.wecom_payload,
            "slack": cls.slack_payload,
        }
        builder = builders.get(channel)
        if builder is None:
            raise ValueError(f"未知 Webhook 类型：{channel}")
        return builder(text)

    def send(self, url: str, payload: dict[str, Any]) -> bool:
        try:
            response = self.client.post(url, json=payload)
            return response.status_code < 300
        except httpx.HTTPError:
            return False

    def build_payloads_for_test(self, settings) -> list[dict[str, Any]]:
        """不发送，只构造 payload（供 --webhook-test 与 UI 预览）。"""
        payloads = []
        for url in settings.webhook_urls:
            payloads.append(
                {
                    "url": url,
                    "channel": settings.webhook_type,
                    "payload": self.payload_for(settings.webhook_type, "测试消息"),
                }
            )
        return payloads

    def send_report_by_id(
        self,
        report_id: str,
        settings,
        agent_repository,
    ) -> list[str]:
        """按 report_id 推送报告摘要；返回实际送达的 URL 列表。"""
        if not settings.webhook_urls or not settings.webhook_type:
            return []
        report = agent_repository.get_report(report_id)
        text = report.summary
        delivered = []
        for url in settings.webhook_urls:
            try:
                payload = self.payload_for(settings.webhook_type, text)
            except ValueError:
                continue
            if self.send(url, payload):
                delivered.append(url)
        return delivered
