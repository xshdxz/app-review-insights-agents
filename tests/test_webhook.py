import httpx

from app_review_insights.monitor.webhook import WebhookSender


def test_payload_formats():
    text = "报告摘要"
    assert WebhookSender.feishu_payload(text) == {"msg_type": "text", "content": {"text": text}}
    assert WebhookSender.dingtalk_payload(text) == {"msgtype": "text", "text": {"content": text}}
    assert WebhookSender.wecom_payload(text) == {"msgtype": "text", "text": {"content": text}}
    assert WebhookSender.slack_payload(text) == {"text": text}


def test_send_success():
    def handler(request):
        return httpx.Response(200, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    sender = WebhookSender(client=client)
    assert sender.send("https://hooks.example.com/x", {"text": "hi"}) is True


def test_send_failure_returns_false():
    def handler(request):
        return httpx.Response(500, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    sender = WebhookSender(client=client)
    assert sender.send("https://hooks.example.com/x", {"text": "hi"}) is False


def test_build_payloads_for_test():
    class _Settings:
        webhook_type = "feishu"
        webhook_urls = ["https://hooks.example.com/feishu"]

    sender = WebhookSender()
    payloads = sender.build_payloads_for_test(_Settings())
    assert payloads[0]["url"] == "https://hooks.example.com/feishu"
    assert payloads[0]["payload"]["msg_type"] == "text"


def test_send_report_by_id(tmp_path):
    from datetime import UTC, datetime

    from app_review_insights.models import MonitorReport
    from app_review_insights.storage.agent_repository import AgentRepository

    agent_repo = AgentRepository(tmp_path / "agent.sqlite3")
    agent_repo.save_report(
        MonitorReport(
            report_id="rep1",
            agent_run_id="a1",
            app_url="https://apps.apple.com/us/app/x/id1",
            goal="g",
            markdown="# r",
            summary="报告摘要",
            findings_count=2,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )

    def handler(request):
        return httpx.Response(200, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))

    class _Settings:
        webhook_type = "slack"
        webhook_urls = ["https://hooks.example.com/slack"]

    sender = WebhookSender(client=client)
    delivered = sender.send_report_by_id("rep1", _Settings(), agent_repo)
    assert delivered == ["https://hooks.example.com/slack"]
