"""Prompt 版本化：版本号与文本指纹必须成对更新。

设计要点：版本号只进**运行记录与评测报告**，不进**请求文本**——所以它不改变录制键
（键只由 schema + system + user 决定），也就不会作废 `data/recordings/demo-replay.json`。
"""

from __future__ import annotations

from datetime import UTC, datetime

from app_review_insights.llm.prompts import (
    PROMPT_VERSIONS,
    prompt_fingerprint,
    prompt_version,
)
from app_review_insights.models import AnalysisRequest, Review, SourceType
from app_review_insights.pipeline.orchestrator import AnalysisOrchestrator, PipelineServices
from app_review_insights.storage import RunRepository

#: 版本串与 prompt 文本指纹的钉死值。
#:
#: **这条断言红了，说明你改了 prompt 文本。** 请依次：① 把 `PROMPT_VERSIONS` 里对应阶段的
#: 版本号 +1；② 更新下面的钉死值；③ 在 CHANGELOG 记一笔、并按新版本重取评测基线。
#: 改文本本身不会作废录制件（版本号不进请求），但会改变模型输出分布。
_PINNED_VERSION = "batch-v1+consolidate-v1+evidence_audit-v1+planning-v1+test_generation-v1"
_PINNED_FINGERPRINT = "9e70a08b5eef"


def test_prompt_version_covers_every_stage():
    assert set(PROMPT_VERSIONS) == {
        "batch",
        "consolidate",
        "evidence_audit",
        "planning",
        "test_generation",
    }
    assert prompt_version() == _PINNED_VERSION


def test_prompt_fingerprint_is_pinned_to_the_current_text():
    assert prompt_fingerprint() == _PINNED_FINGERPRINT, (
        "prompt 文本变了：请 bump PROMPT_VERSIONS 并更新本测试里的钉死值。"
        "版本号与指纹必须成对更新，否则「哪次运行用了哪套 prompt」无法追溯。"
    )


def test_run_record_carries_the_prompt_version(tmp_path):
    """运行记录要能自证「哪套 prompt 跑出来的」，且要真的落盘（JSON 往返之后仍在）。"""
    repository = RunRepository(tmp_path / "runs.sqlite3")
    services = PipelineServices(repository=repository, batch_analyzer=None)
    reviews = [
        Review(
            review_id="r1",
            app_id="a1",
            content_original="续费日期没有提前说明。",
            rating=2,
            published_at=datetime(2026, 6, 1, tzinfo=UTC),
            source="test",
        )
    ]

    run = AnalysisOrchestrator(services).start(
        AnalysisRequest(source_type=SourceType.JSON, analysis_goal="分析订阅转化"),
        imported_reviews=reviews,
    )

    stored = repository.get_run(run.run_id)
    assert stored.prompt_version == prompt_version()
    assert stored.prompt_fingerprint == prompt_fingerprint()
