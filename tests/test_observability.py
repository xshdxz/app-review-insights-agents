"""观测口径：分位数只有一处实现，阶段耗时真的落盘。

"慢在哪一段"以前只能靠猜——模型调用耗时记在 `model_usage` 里，但阶段本身（清洗、归并、
校验）没有耗时记录。这里守住两件事：分位数的口径，以及阶段耗时确实被写进去了。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app_review_insights.models import AnalysisRequest, Review, RunStatus, SourceType, Stage
from app_review_insights.observability import percentile, summarize
from app_review_insights.pipeline.orchestrator import AnalysisOrchestrator, PipelineServices
from app_review_insights.storage import RunRepository

# ── 分位数口径 ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("values", "q", "expected"),
    [
        ([1, 2, 3, 4], 0.5, 2.5),
        ([1, 2, 3, 4], 0.95, 3.85),
        ([5], 0.95, 5.0),
        ([], 0.5, 0.0),
        ([3, 1, 2], 0.0, 1.0),
        ([3, 1, 2], 1.0, 3.0),
    ],
)
def test_percentile_uses_linear_interpolation(values, q, expected):
    assert percentile(values, q) == pytest.approx(expected)


def test_percentile_rejects_out_of_range_q():
    with pytest.raises(ValueError):
        percentile([1, 2], 1.5)


def test_summarize_reports_count_p50_p95_max():
    summary = summarize([10, 20, 30, 40])

    assert summary == {"count": 4, "p50": 25.0, "p95": 38.5, "max": 40.0}


def test_summarize_of_nothing_is_all_zeros():
    assert summarize([]) == {"count": 0, "p50": 0.0, "p95": 0.0, "max": 0.0}


# ── 阶段耗时落盘 ─────────────────────────────────────────────────────────────


def _review() -> Review:
    return Review(
        review_id="r1",
        app_id="a1",
        content_original="续费日期没有提前说明。",
        rating=2,
        published_at=datetime(2026, 6, 1, tzinfo=UTC),
        source="test",
    )


def test_completed_stage_timings_are_recorded_for_every_stage(tmp_path):
    """跑一次（在没有模型的情况下停在中途），已推进过的阶段必须都留下耗时记录。"""
    repository = RunRepository(tmp_path / "runs.sqlite3")
    services = PipelineServices(repository=repository, batch_analyzer=None)

    run = AnalysisOrchestrator(services).start(
        AnalysisRequest(source_type=SourceType.JSON, analysis_goal="分析订阅转化"),
        imported_reviews=[_review()],
    )

    assert run.status is RunStatus.WAITING
    summary = repository.stage_timing_summary()
    assert {"collect", "clean", "analyze_batches"} <= set(summary), (
        f"阶段耗时缺记录：{sorted(summary)}"
    )
    for stage, stats in summary.items():
        assert stats["count"] >= 1, stage
        assert stats["p95"] >= stats["p50"] > 0, f"{stage} 的耗时必须为正数"


def test_stage_timings_are_grouped_and_windowed(tmp_path):
    repository = RunRepository(tmp_path / "runs.sqlite3")
    now = datetime.now(UTC)
    for offset, duration in ((0, 100.0), (1, 300.0)):
        repository.record_stage_timing(
            "run-1",
            Stage.CLEAN,
            duration,
            started_at=now - timedelta(minutes=offset),
            ended_at=now - timedelta(minutes=offset),
        )
    repository.record_stage_timing(
        "run-2",
        Stage.PLAN,
        50.0,
        started_at=now - timedelta(days=30),
        ended_at=now - timedelta(days=30),
    )

    all_stages = repository.stage_timing_summary()
    recent = repository.stage_timing_summary(since=now - timedelta(hours=1))

    assert all_stages["clean"]["count"] == 2
    assert all_stages["clean"]["max"] == 300.0
    assert set(recent) == {"clean"}, "窗口之外的样本不该被算进来"
    assert recent["clean"]["count"] == 2


def test_pruning_a_run_removes_its_stage_timings(tmp_path):
    """阶段耗时跟着运行走，否则它就是一个只涨不跌的表。"""
    repository = RunRepository(tmp_path / "runs.sqlite3")
    old = datetime.now(UTC) - timedelta(days=400)
    repository.record_stage_timing("run-old", Stage.CLEAN, 10.0, started_at=old, ended_at=old)
    from app_review_insights.models import RunRecord

    repository.save_run(
        RunRecord(
            run_id="run-old",
            request=AnalysisRequest(source_type=SourceType.JSON, analysis_goal="分析订阅转化"),
            current_stage=Stage.COMPLETE,
            status=RunStatus.COMPLETED,
            created_at=old,
            updated_at=old,
        )
    )

    removed = repository.prune_runs(older_than_days=90)
    repository.vacuum()

    assert removed == 1
    assert repository.stage_timing_summary() == {}


def test_model_latency_summary_groups_by_stage(tmp_path):
    from app_review_insights.llm.usage import build_usage

    repository = RunRepository(tmp_path / "runs.sqlite3")
    for stage, latency in (("eval", 100.0), ("eval", 300.0), ("plan", 50.0)):
        usage = build_usage(
            model="deepseek-chat",
            prompt_tokens=10,
            completion_tokens=10,
            latency_ms=latency,
            stage=stage,
        )
        repository.record_model_usage(usage)

    summary = repository.model_latency_summary()

    assert summary["eval"]["count"] == 2
    assert summary["eval"]["max"] == 300.0
    assert summary["plan"]["count"] == 1
