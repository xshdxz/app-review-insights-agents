"""模型调用用量与成本计量。

LLM 应用的生命线：一次分析花了多少 token、多少钱，必须有账可查。
此前全项目没有任何 token / cost 计量，上线等于不可控开支。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from app_review_insights.errors import RecoverableModelError
from app_review_insights.llm.provider import DeepSeekProvider
from app_review_insights.llm.usage import (
    MODEL_PRICING,
    build_usage,
    current_run_id,
    current_stage,
    estimate_cost_usd,
)
from app_review_insights.storage.repository import RunRepository


class _Draft(BaseModel):
    ok: bool


def _response(prompt_tokens=100, completion_tokens=50, content='{"ok": true}'):
    return SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
    )


class _FakeClient:
    def __init__(self, response):
        self._response = response
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.calls = 0

    def _create(self, **_kwargs):
        self.calls += 1
        return self._response


# ── 计价 ─────────────────────────────────────────────────────────────────────


def test_estimate_cost_uses_registered_price():
    input_price, output_price = MODEL_PRICING["deepseek-chat"]
    cost = estimate_cost_usd("deepseek-chat", prompt_tokens=1_000_000, completion_tokens=0)
    assert cost == pytest.approx(input_price)


def test_estimate_cost_counts_output_tokens_separately():
    cost = estimate_cost_usd("deepseek-chat", prompt_tokens=0, completion_tokens=1_000_000)
    assert cost == pytest.approx(MODEL_PRICING["deepseek-chat"][1])


def test_estimate_cost_falls_back_for_unknown_model():
    """未登记模型也要能算，不能因为没报价就丢账。"""
    assert estimate_cost_usd("some-new-model", 1000, 1000) > 0


def test_estimate_cost_for_zero_usage_is_zero():
    assert estimate_cost_usd("deepseek-chat", 0, 0) == 0.0


# ── 用量对象 ─────────────────────────────────────────────────────────────────


def test_build_usage_sums_total_and_picks_up_context(monkeypatch):
    token_run = current_run_id.set("run-1")
    token_stage = current_stage.set("analyze_batches")
    try:
        usage = build_usage(
            model="deepseek-chat",
            prompt_tokens=1200,
            completion_tokens=300,
            latency_ms=42.5,
        )
    finally:
        current_run_id.reset(token_run)
        current_stage.reset(token_stage)

    assert usage.total_tokens == 1500
    assert usage.run_id == "run-1"
    assert usage.stage == "analyze_batches"
    assert usage.estimated_cost_usd > 0
    assert usage.created_at.tzinfo is not None


def test_build_usage_explicit_context_wins():
    token = current_run_id.set("from-context")
    try:
        usage = build_usage(
            model="deepseek-chat",
            prompt_tokens=1,
            completion_tokens=1,
            latency_ms=1.0,
            run_id="explicit",
            stage="plan",
        )
    finally:
        current_run_id.reset(token)

    assert usage.run_id == "explicit"
    assert usage.stage == "plan"


# ── provider 上报 ────────────────────────────────────────────────────────────


def test_provider_reports_usage_after_successful_call():
    recorded = []
    provider = DeepSeekProvider(
        client=_FakeClient(_response()),
        model="deepseek-chat",
        usage_recorder=recorded.append,
    )

    provider.generate("系统", "用户", _Draft)

    assert len(recorded) == 1
    assert recorded[0].prompt_tokens == 100
    assert recorded[0].completion_tokens == 50
    assert recorded[0].total_tokens == 150
    assert recorded[0].model == "deepseek-chat"
    assert recorded[0].latency_ms >= 0


def test_provider_without_recorder_still_works():
    provider = DeepSeekProvider(client=_FakeClient(_response()), model="deepseek-chat")
    assert provider.generate("系统", "用户", _Draft).ok is True


def test_recorder_failure_does_not_break_the_pipeline():
    """计量失败绝不能影响主流程，但也必须留下告警而不是静默吞掉。"""

    def _broken(_usage):
        raise RuntimeError("磁盘满了")

    provider = DeepSeekProvider(
        client=_FakeClient(_response()),
        model="deepseek-chat",
        usage_recorder=_broken,
    )

    assert provider.generate("系统", "用户", _Draft).ok is True


def test_provider_reports_usage_on_schema_validation_failure():
    """Schema 校验失败时 API 已经计费，这笔账不能漏。"""
    recorded = []
    provider = DeepSeekProvider(
        client=_FakeClient(_response(content='{"wrong": 1}')),
        model="deepseek-chat",
        max_retries=0,
        retry_delays=(),
        usage_recorder=recorded.append,
    )

    with pytest.raises(RecoverableModelError):
        provider.generate("系统", "用户", _Draft)

    assert len(recorded) == 1
    assert recorded[0].total_tokens == 150


# ── 持久化与汇总 ─────────────────────────────────────────────────────────────


def test_repository_records_and_summarises_usage(tmp_path):
    repository = RunRepository(tmp_path / "runs.sqlite3")

    for prompt, completion in ((100, 50), (200, 100)):
        repository.record_model_usage(
            build_usage(
                model="deepseek-chat",
                prompt_tokens=prompt,
                completion_tokens=completion,
                latency_ms=10.0,
                run_id="run-1",
                stage="analyze_batches",
            )
        )

    summary = repository.model_usage_summary(run_id="run-1")

    assert summary["calls"] == 2
    assert summary["prompt_tokens"] == 300
    assert summary["completion_tokens"] == 150
    assert summary["total_tokens"] == 450
    assert summary["estimated_cost_usd"] > 0


def test_usage_summary_is_scoped_by_run(tmp_path):
    repository = RunRepository(tmp_path / "runs.sqlite3")
    for run_id in ("run-1", "run-2"):
        repository.record_model_usage(
            build_usage(
                model="deepseek-chat",
                prompt_tokens=10,
                completion_tokens=5,
                latency_ms=1.0,
                run_id=run_id,
            )
        )

    assert repository.model_usage_summary(run_id="run-1")["calls"] == 1
    assert repository.model_usage_summary()["calls"] == 2


# ── UI 展示 ──────────────────────────────────────────────────────────────────


def test_usage_caption_is_hidden_when_no_calls():
    """离线演示等零调用场景不该显示"成本 $0.0000"这种噪音。"""
    from app_review_insights.ui.components import format_usage_caption

    assert format_usage_caption(None) is None
    assert format_usage_caption({"calls": 0, "total_tokens": 0, "estimated_cost_usd": 0.0}) is None


def test_usage_caption_shows_tokens_calls_and_cost():
    from app_review_insights.ui.components import format_usage_caption

    caption = format_usage_caption(
        {"calls": 7, "total_tokens": 12_345, "estimated_cost_usd": 0.0123}
    )

    assert caption is not None
    assert "12345 tokens" in caption
    assert "7 次调用" in caption
    assert "0.0123" in caption


def test_usage_summary_by_stage_breaks_down_cost_drivers(tmp_path):
    repository = RunRepository(tmp_path / "runs.sqlite3")
    for stage, prompt in (("analyze_batches", 1000), ("plan", 10)):
        repository.record_model_usage(
            build_usage(
                model="deepseek-chat",
                prompt_tokens=prompt,
                completion_tokens=0,
                latency_ms=1.0,
                run_id="run-1",
                stage=stage,
            )
        )

    by_stage = repository.model_usage_by_stage(run_id="run-1")

    assert by_stage["analyze_batches"]["total_tokens"] == 1000
    assert by_stage["plan"]["total_tokens"] == 10
