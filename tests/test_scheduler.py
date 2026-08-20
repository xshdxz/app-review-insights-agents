from datetime import UTC, datetime

import pytest
from apscheduler.triggers.cron import CronTrigger

from app_review_insights.models import AgentRun, AgentRunStatus, MonitorJob
from app_review_insights.monitor.scheduler import MonitorScheduler
from app_review_insights.storage.agent_repository import AgentRepository


@pytest.fixture
def agent_repo(tmp_path):
    return AgentRepository(tmp_path / "agent.sqlite3")


class _FakeStack:
    def __init__(self):
        self.runs = []

    def run(self, goal, app_url, require_approval=False):
        self.runs.append((goal, app_url, require_approval))
        return AgentRun(
            run_id="agent-1",
            goal=goal,
            app_url=app_url,
            status=AgentRunStatus.COMPLETED,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        )


def test_cron_trigger_parses():
    trigger = CronTrigger.from_crontab("0 9 * * *")
    assert trigger is not None


def _job(job_id, enabled=True):
    return MonitorJob(
        job_id=job_id,
        name="每日监控",
        app_url="https://apps.apple.com/us/app/x/id1",
        goal="监控订阅转化",
        cron="0 9 * * *",
        enabled=enabled,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_scheduler_loads_enabled_jobs_only(agent_repo):
    agent_repo.save_job(_job("j1", enabled=True))
    agent_repo.save_job(_job("j2", enabled=False))
    scheduler = MonitorScheduler(
        agent_repository=agent_repo,
        run_job_fn=lambda *args: None,
        timezone="UTC",
    )
    scheduler.start()
    jobs = scheduler._scheduler.get_jobs()
    assert [job.id for job in jobs] == ["job-j1"]
    scheduler.shutdown()


def test_run_job_updates_last_status(agent_repo):
    agent_repo.save_job(_job("j1"))
    fake = _FakeStack()
    scheduler = MonitorScheduler(
        agent_repository=agent_repo,
        run_job_fn=fake.run,
        timezone="UTC",
    )
    scheduler._run_job("j1")
    job = agent_repo.get_job("j1")
    assert job.last_status == "completed"
    assert job.last_run_at is not None
