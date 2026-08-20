from datetime import UTC, datetime

import pytest

from app_review_insights.models import (
    AgentRun,
    AgentRunStatus,
    MonitorJob,
    MonitorReport,
    Review,
)
from app_review_insights.storage.agent_repository import AgentRepository


@pytest.fixture
def repo(tmp_path):
    return AgentRepository(tmp_path / "agent.sqlite3")


def _review(review_id: str, app_id: str, content: str, platform: str = "app-store") -> Review:
    return Review(
        review_id=review_id,
        app_id=app_id,
        content_original=content,
        rating=3,
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
        source="test",
        platform=platform,
    )


def test_agent_run_roundtrip(repo):
    run = AgentRun(
        run_id="a1",
        goal="分析订阅转化",
        app_url="https://apps.apple.com/us/app/x/id1",
        status=AgentRunStatus.PENDING,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    repo.save_agent_run(run)
    loaded = repo.get_agent_run("a1")
    assert loaded.goal == "分析订阅转化"
    assert loaded.status == AgentRunStatus.PENDING


def test_agent_run_list_by_status(repo):
    for i in range(3):
        repo.save_agent_run(
            AgentRun(
                run_id=f"r{i}",
                goal="g",
                app_url="https://apps.apple.com/us/app/x/id1",
                status=AgentRunStatus.WAITING_APPROVAL if i == 0 else AgentRunStatus.COMPLETED,
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
                updated_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
    waiting = repo.list_agent_runs(status=AgentRunStatus.WAITING_APPROVAL)
    assert [r.run_id for r in waiting] == ["r0"]


def test_monitor_job_crud(repo):
    job = MonitorJob(
        job_id="j1",
        name="每日监控",
        app_url="https://apps.apple.com/us/app/x/id1",
        goal="监控订阅转化口碑",
        cron="0 9 * * *",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    repo.save_job(job)
    assert repo.get_job("j1").name == "每日监控"
    assert len(repo.list_jobs()) == 1
    repo.delete_job("j1")
    assert repo.list_jobs() == []


def test_report_roundtrip_and_latest(repo):
    report = MonitorReport(
        report_id="rep1",
        agent_run_id="a1",
        app_url="https://apps.apple.com/us/app/x/id1",
        goal="g",
        markdown="# 报告",
        summary="摘要",
        findings_count=3,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    repo.save_report(report)
    assert repo.get_report("rep1").summary == "摘要"
    assert repo.latest_report("https://apps.apple.com/us/app/x/id1").report_id == "rep1"
    assert repo.latest_report("https://other.example") is None


def test_corpus_upsert_and_search(repo):
    repo.upsert_corpus(_review("v1", "app-a", "订阅太贵了，续费不划算"))
    repo.upsert_corpus(_review("v2", "app-a", "界面很漂亮"))
    repo.upsert_corpus(_review("v3", "app-b", "订阅流程顺畅"))
    hits = repo.search_corpus("订阅", app_ids=["app-a"], limit=10)
    assert {h["review_id"] for h in hits} == {"v1"}
    assert repo.app_ids() == ["app-a", "app-b"]


def test_corpus_social_platform_tagged(repo):
    repo.upsert_corpus(_review("s1", "app-a", "有人在讨论这个 App", platform="social"))
    hits = repo.search_corpus("讨论", app_ids=["app-a"], limit=10)
    assert hits[0]["platform"] == "social"


def test_delete_app(repo):
    repo.upsert_corpus(_review("v1", "app-a", "内容一"))
    repo.upsert_corpus(_review("v2", "app-b", "内容二"))
    assert repo.delete_app("app-a") == 1
    assert repo.app_ids() == ["app-b"]


def test_delete_app_removes_embeddings(repo, tmp_path):
    import sqlite3

    repo.upsert_corpus(_review("v1", "app-a", "内容一"))
    repo.upsert_embedding("v1", [1.0, 0.0, 0.0])
    assert repo.delete_app("app-a") == 1
    connection = sqlite3.connect(tmp_path / "agent.sqlite3")
    try:
        count = connection.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    finally:
        connection.close()
    assert count == 0


def test_corpus_cjk_phrase_precision(repo):
    # 「订阅」不应命中订/阅分散在文中不同位置的评论
    repo.upsert_corpus(_review("v1", "app-a", "订阅太贵了"))
    repo.upsert_corpus(_review("v2", "app-a", "我订了酒店，阅读体验不错"))
    hits = repo.search_corpus("订阅", app_ids=["app-a"], limit=10)
    assert {h["review_id"] for h in hits} == {"v1"}


def test_corpus_cjk_multiword_phrase_boundaries(repo):
    # 「订阅 价格」是两个词：不应要求四字连续，应命中词分散的评论
    repo.upsert_corpus(_review("v1", "app-a", "订阅太贵了，价格不透明"))
    repo.upsert_corpus(_review("v2", "app-a", "价格划算，订阅体验也好"))
    hits = repo.search_corpus("订阅 价格", app_ids=["app-a"], limit=10)
    assert {h["review_id"] for h in hits} == {"v1", "v2"}


def test_corpus_punctuation_only_query_no_match(repo):
    # 纯标点查询不应崩溃，也不应命中任何评论（与 "空则匹配不到任何行" 契约一致）
    repo.upsert_corpus(_review("v1", "app-a", "订阅太贵了"))
    hits = repo.search_corpus("，。！", app_ids=["app-a"], limit=10)
    assert hits == []


def test_corpus_cjk_query_with_fullwidth_punctuation(repo):
    # 全角标点作为 run 边界：CJK 片段仍分段匹配
    repo.upsert_corpus(_review("v1", "app-a", "太贵了，续费不划算"))
    hits = repo.search_corpus("太贵了，续费", app_ids=["app-a"], limit=10)
    assert {h["review_id"] for h in hits} == {"v1"}


def test_corpus_mixed_cjk_latin_query(repo):
    # 中英粘连：CJK 段短语化 + 拉丁段独立匹配
    repo.upsert_corpus(_review("v1", "app-a", "订阅App 后闪退"))
    hits = repo.search_corpus("订阅App", app_ids=["app-a"], limit=10)
    assert {h["review_id"] for h in hits} == {"v1"}


def test_corpus_mixed_latin_cjk_query(repo):
    # 拉丁在前、CJK 在后同样成立（索引侧拉丁-CJK 边界分段）
    repo.upsert_corpus(_review("v1", "app-a", "App闪退，广告太多"))
    hits = repo.search_corpus("App闪退", app_ids=["app-a"], limit=10)
    assert {h["review_id"] for h in hits} == {"v1"}


def test_fts5_available():
    from app_review_insights.storage.agent_repository import fts5_available

    assert fts5_available() is True
