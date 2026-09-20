"""三层追踪：装了要真的嵌套，没装必须完全无感。

CI 不装 `[observability]` extra，本地装了——两条路径都有测试，而且 CI 每次跑都在验证
"降级仍然全绿"这一半。
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime

import pytest

from app_review_insights import tracing
from app_review_insights.models import AnalysisRequest, Review, RunStatus, SourceType
from app_review_insights.pipeline.orchestrator import AnalysisOrchestrator, PipelineServices
from app_review_insights.storage import RunRepository


class _FakeSpan:
    def __init__(self, name: str) -> None:
        self.name = name
        self.attributes: dict[str, object] = {}
        self.ended = False

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value

    def end(self) -> None:
        self.ended = True


class _FakeTracer:
    def __init__(self) -> None:
        self.spans: list[_FakeSpan] = []

    def start_span(self, name: str) -> _FakeSpan:
        span = _FakeSpan(name)
        self.spans.append(span)
        return span

    @contextmanager
    def start_as_current_span(self, name: str):
        span = _FakeSpan(name)
        self.spans.append(span)
        try:
            yield span
        finally:
            span.end()


# ── 降级路径 ─────────────────────────────────────────────────────────────────


def test_span_is_a_noop_without_a_tracer(monkeypatch):
    monkeypatch.setattr(tracing, "_tracer", None)

    with tracing.span("whatever", key="value") as current:
        assert current is None


def test_start_and_end_span_are_noops_without_a_tracer(monkeypatch):
    monkeypatch.setattr(tracing, "_tracer", None)

    handle = tracing.start_span("pipeline.stage", stage="clean")
    tracing.end_span(handle)  # 不该抛

    assert handle is None


def test_configure_tracing_reports_false_without_the_optional_dependency(monkeypatch):
    monkeypatch.setattr(tracing, "_OTEL_AVAILABLE", False)
    monkeypatch.setattr(tracing, "_tracer", None)

    assert tracing.configure_tracing() is False


def test_tracer_failures_never_reach_the_caller(monkeypatch):
    """观测能力不能反过来搞挂主流程。"""

    class _Exploding:
        def start_span(self, name):
            raise RuntimeError("tracer exploded")

    monkeypatch.setattr(tracing, "_tracer", _Exploding())

    assert tracing.start_span("x") is None


# ── 启用路径（用假 tracer 验证被调用与嵌套顺序）─────────────────────────────


def test_start_span_sets_attributes_and_ends_the_span(monkeypatch):
    tracer = _FakeTracer()
    monkeypatch.setattr(tracing, "_tracer", tracer)

    handle = tracing.start_span("pipeline.stage", stage="clean")
    tracing.end_span(handle)

    assert [span.name for span in tracer.spans] == ["pipeline.stage"]
    assert tracer.spans[0].attributes == {"stage": "clean"}
    assert tracer.spans[0].ended is True


def _review() -> Review:
    return Review(
        review_id="r1",
        app_id="a1",
        content_original="续费日期没有提前说明。",
        rating=2,
        published_at=datetime(2026, 6, 1, tzinfo=UTC),
        source="test",
    )


def test_orchestrator_emits_a_run_span_and_one_span_per_stage(tmp_path, monkeypatch):
    """run 的 span 必须包住 stage 的 span —— 否则时间轴上读不出归属关系。"""
    tracer = _FakeTracer()
    monkeypatch.setattr(tracing, "_tracer", tracer)

    repository = RunRepository(tmp_path / "runs.sqlite3")
    services = PipelineServices(repository=repository, batch_analyzer=None)

    run = AnalysisOrchestrator(services).start(
        AnalysisRequest(source_type=SourceType.JSON, analysis_goal="分析订阅转化"),
        imported_reviews=[_review()],
    )

    assert run.status is RunStatus.WAITING
    names = [span.name for span in tracer.spans]
    assert "analysis.run" in names
    stage_spans = [span for span in tracer.spans if span.name == "pipeline.stage"]
    assert [span.attributes["stage"] for span in stage_spans] == [
        "collect",
        "clean",
        "analyze_batches",
    ]
    assert all(span.ended for span in stage_spans), "每个 stage 的 span 都必须被结束"


def test_orchestrator_works_unchanged_without_tracing(tmp_path, monkeypatch):
    """降级路径的行为必须与启用追踪时**完全一致**（只是没有 span）。"""
    monkeypatch.setattr(tracing, "_tracer", None)
    repository = RunRepository(tmp_path / "runs.sqlite3")
    services = PipelineServices(repository=repository, batch_analyzer=None)

    run = AnalysisOrchestrator(services).start(
        AnalysisRequest(source_type=SourceType.JSON, analysis_goal="分析订阅转化"),
        imported_reviews=[_review()],
    )

    assert run.status is RunStatus.WAITING
    assert repository.stage_timing_summary(), "没有追踪时阶段耗时照常记录"


# ── 真 SDK 下的嵌套（没装 extra 时跳过；CI 不装，正好覆盖降级那一半）───────────


@pytest.mark.skipif(not tracing.otel_available(), reason="需要可选的 observability extra")
def test_real_otel_spans_are_actually_nested(tmp_path, monkeypatch):
    """用真 SDK 验证父子关系——假 tracer 只能证明"被调用了"，证明不了嵌套。

    "三层追踪"如果只是三个平级 span，在时间轴上读不出"这段时间花在哪个阶段的哪次调用上"，
    那就等于没做。所以这条断言查的是 parent span_id，而不是名字。
    """
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "_tracer", provider.get_tracer("ari-test"))

    repository = RunRepository(tmp_path / "runs.sqlite3")
    services = PipelineServices(repository=repository, batch_analyzer=None)
    AnalysisOrchestrator(services).start(
        AnalysisRequest(source_type=SourceType.JSON, analysis_goal="分析订阅转化"),
        imported_reviews=[_review()],
    )
    provider.shutdown()

    finished = exporter.get_finished_spans()
    run_span = next((span for span in finished if span.name == "analysis.run"), None)
    stage_spans = [span for span in finished if span.name == "pipeline.stage"]

    assert run_span is not None, [span.name for span in finished]
    assert stage_spans, "阶段 span 缺失"
    run_span_id = run_span.context.span_id
    orphans = [
        span.attributes.get("stage")
        for span in stage_spans
        if span.parent is None or span.parent.span_id != run_span_id
    ]
    assert not orphans, f"这些阶段 span 没有挂在 run span 下面：{orphans}"
