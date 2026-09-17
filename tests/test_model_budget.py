"""模型调用预算熔断。

没有预算的 LLM 应用等于不可控开支：一次误操作的分析可能烧掉整月额度。
超预算时必须以**可续跑**的方式停下（已完成的工作全部保留），而不是把进度丢掉。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from app_review_insights.errors import ModelBudgetExceeded, RecoverableModelError
from app_review_insights.llm.budget import make_budget_guard, start_of_today_utc
from app_review_insights.llm.provider import DeepSeekProvider
from app_review_insights.llm.usage import build_usage, current_run_id
from app_review_insights.storage.repository import RunRepository


class _Draft(BaseModel):
    ok: bool


def _response(content='{"ok": true}'):
    return SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50),
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
    )


class _FakeClient:
    def __init__(self, response):
        self._response = response
        self.calls = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **_kwargs):
        self.calls += 1
        return self._response


# ── 预算守卫 ─────────────────────────────────────────────────────────────────


def test_no_budget_configured_returns_none():
    """未配置预算时不安装守卫，避免无谓的每调用一次查询。"""
    assert make_budget_guard(lambda run_id, since: 0.0, per_run_usd=0, per_day_usd=0) is None


def test_per_run_budget_allows_spending_under_the_limit():
    guard = make_budget_guard(lambda run_id, since: 0.5, per_run_usd=1.0)
    assert guard is not None
    guard()  # 不抛


def test_per_run_budget_raises_when_limit_reached():
    guard = make_budget_guard(lambda run_id, since: 1.0, per_run_usd=1.0)
    assert guard is not None
    with pytest.raises(ModelBudgetExceeded):
        guard()


def test_per_day_budget_raises_when_limit_reached():
    guard = make_budget_guard(lambda run_id, since: 5.0, per_day_usd=5.0)
    assert guard is not None
    with pytest.raises(ModelBudgetExceeded):
        guard()


def test_day_budget_only_counts_usage_since_midnight():
    seen: list = []

    def _spent(run_id, since):
        seen.append((run_id, since))
        return 0.0

    guard = make_budget_guard(_spent, per_day_usd=1.0)
    assert guard is not None
    guard()

    run_id, since = seen[0]
    assert run_id is None  # 日预算跨运行统计
    assert since == start_of_today_utc()


def test_run_budget_scopes_to_current_run():
    seen: list = []

    def _spent(run_id, since):
        seen.append((run_id, since))
        return 0.0

    guard = make_budget_guard(_spent, per_run_usd=1.0)
    assert guard is not None
    token = current_run_id.set("run-42")
    try:
        guard()
    finally:
        current_run_id.reset(token)

    assert seen[0] == ("run-42", None)


def test_budget_error_is_recoverable_so_work_is_kept():
    """必须继承 RecoverableModelError：流水线要停在检查点上，而不是丢弃进度。"""
    assert issubclass(ModelBudgetExceeded, RecoverableModelError)


def test_start_of_today_is_midnight_utc():
    moment = datetime(2026, 9, 17, 15, 30, tzinfo=UTC)
    assert start_of_today_utc(moment) == datetime(2026, 9, 17, 0, 0, tzinfo=UTC)
    assert start_of_today_utc(moment) < moment < start_of_today_utc(moment) + timedelta(days=1)


# ── provider 接入 ────────────────────────────────────────────────────────────


def test_provider_checks_budget_before_each_attempt():
    checks = []

    def _guard():
        checks.append(1)

    client = _FakeClient(_response())
    provider = DeepSeekProvider(client=client, model="deepseek-chat", budget_check=_guard)

    provider.generate("系统", "用户", _Draft)

    assert len(checks) == 1
    assert client.calls == 1


def test_provider_does_not_call_model_when_budget_exhausted():
    def _guard():
        raise ModelBudgetExceeded("预算用尽")

    client = _FakeClient(_response())
    provider = DeepSeekProvider(client=client, model="deepseek-chat", budget_check=_guard)

    with pytest.raises(ModelBudgetExceeded):
        provider.generate("系统", "用户", _Draft)

    assert client.calls == 0  # 一分钱都没花


# ── 仓库按时间窗汇总 ─────────────────────────────────────────────────────────


def test_summary_supports_since_window(tmp_path):
    repository = RunRepository(tmp_path / "runs.sqlite3")
    old = build_usage(
        model="deepseek-chat",
        prompt_tokens=1_000_000,
        completion_tokens=0,
        latency_ms=1.0,
        run_id="old-run",
    ).model_copy(update={"created_at": datetime(2020, 1, 1, tzinfo=UTC)})
    fresh = build_usage(
        model="deepseek-chat",
        prompt_tokens=10,
        completion_tokens=0,
        latency_ms=1.0,
        run_id="new-run",
    )
    repository.record_model_usage(old)
    repository.record_model_usage(fresh)

    all_time = repository.model_usage_summary()
    today = repository.model_usage_summary(since=start_of_today_utc())

    assert all_time["calls"] == 2
    assert today["calls"] == 1
    assert today["total_tokens"] == 10
