"""录制脚本的离线行为测试。

替身装配全程离线：预置响应按流水线实际调用顺序排队，不对应任何真实模型调用。
"""

from __future__ import annotations

from pathlib import Path

from app_review_insights.batching import make_review_batches
from app_review_insights.cleaning import clean_reviews
from app_review_insights.config import Settings, load_settings
from app_review_insights.input_parsing import import_reviews
from app_review_insights.llm import schemas as llm_schemas
from app_review_insights.llm.recording import RECORDING_MODE, load_recording
from app_review_insights.models import Review, RunStatus
from app_review_insights.storage.cache import SAMPLE_PATH
from scripts.record_demo import record_demo


def test_record_demo_writes_labeled_recording(tmp_path: Path, monkeypatch):
    destination = tmp_path / "demo-replay.json"
    monkeypatch.setenv("MODEL_RECORD_PATH", str(destination))
    # 隔离运行库：脚本默认写项目的 data/runs/，测试不碰仓库内数据
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "runs.sqlite3"))

    run = record_demo(
        sample_path=SAMPLE_PATH,
        destination=destination,
        services_factory=_recording_services,
    )

    document = load_recording(destination)
    # 只断言「非空」不够：半途停在 waiting_for_model 的录制同样非空，
    # 但回放会从缺内容的那一步起全部未命中。
    assert run.status is RunStatus.COMPLETED, "流水线必须跑完"
    # 比「非空」强的是覆盖范围：流水线的每一类模型调用一个都不能少。
    # 用集合而不是序列——批次数量随批次上限与样例规模变化（样例增长后正确地录出
    # 6 条），钉死 5 元素序列会对着正确行为变红；调用顺序由 _fake_responses()
    # 的构造顺序表达。
    # 按模块引用 Schema 类而不是直接导入：TestCasePlanResult 这类名字被绑定到
    # 测试模块的命名空间后，pytest 会把它当测试类去收集并报警告。
    assert {entry.schema_name for entry in document.entries} == {
        llm_schemas.BatchAnalysisResult.__name__,
        llm_schemas.ConsolidationResult.__name__,
        llm_schemas.EvidenceAuditResult.__name__,
        llm_schemas.RequirementPlanResult.__name__,
        llm_schemas.TestCasePlanResult.__name__,
    }, "录制必须覆盖流水线的每一次模型调用，否则回放中途未命中"
    # 集合看不出「漏录一条」或「多录一条」，再用替身条数钉一遍：
    # 排队的预置响应必须一条不剩地被消费并落盘
    assert len(document.entries) == len(_fake_responses()), "每条预置响应都应恰好录成一条条目"
    assert document.mode == RECORDING_MODE
    assert document.is_live is False
    assert document.entries, "录制文件必须至少含一次模型调用"
    assert document.input_fingerprint


def test_record_demo_refuses_without_usable_key(tmp_path: Path, monkeypatch):
    # 隔离项目根 .env：真配了密钥时 main() 会真的发起一次录制，绝不能让它跑起来
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MODEL_ENABLED", "true")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    from scripts.record_demo import DEFAULT_DESTINATION, main

    # 前提断言必须先于 main()：这条用例的全部意义是「没有密钥就不花钱」，
    # 少了它，守护一旦失效，表现是花掉真钱而不是变红。
    assert load_settings().model_available is False

    # 这次运行真会写的是 DEFAULT_DESTINATION（绝对路径，指向仓库 data/recordings/），
    # 不是 CWD 下的相对路径。故断言「本次没有写出或改写录制文件」而不是「该文件不存在」——
    # Step 5 的真实录制件合法地住在那个路径上，写成 not exists 会反过来变红。
    # 用 (存在, 大小, mtime) 三元组而不是单看存在性：录制件已在仓库里时，
    # 「被一次真实运行整份覆盖」不改变存在性，只有三元组能发现。
    def _artifact_state() -> tuple[bool, int, int]:
        if not DEFAULT_DESTINATION.exists():
            return (False, 0, 0)
        info = DEFAULT_DESTINATION.stat()
        return (True, info.st_size, info.st_mtime_ns)

    state_before = _artifact_state()
    assert main([]) == 1
    assert _artifact_state() == state_before, "拒绝路径不得写出或改写录制文件"
    # CWD 之下（chdir 到 tmp_path 后）同样不许冒出任何文件
    assert not (tmp_path / "data" / "recordings" / "demo-replay.json").exists()


def _cleaned_sample_reviews() -> list[Review]:
    """样例经清洗后的评论；替身引用的 review_id 必须真实存在于流水线输入。"""
    reviews = import_reviews(SAMPLE_PATH.read_bytes(), SAMPLE_PATH.name, app_id="demo")
    return clean_reviews(reviews).reviews


def _fake_responses() -> list[dict]:
    """按流水线实际调用顺序排队的预置响应，全程离线。

    顺序：每批一次批次分析 → 归并 → 证据审计 → 需求 → 每条需求一次用例。
    字段一律取自 llm/schemas.py 的真实定义：BatchAnalysisResult 的字段是
    findings / review_summaries / batch_limitations——写成 summaries 会被 pydantic
    静默丢弃，录出来的回放就是空的，且没有任何测试会发现。
    批次数量由样例清洗后的条数与配置的批次上限共同决定，这里按同一套 batching
    规则算出，替身数量与实际调用次数因此不会错位（少了会直接报校验失败）。
    """
    settings = load_settings()
    reviews = _cleaned_sample_reviews()
    supporting = [review.review_id for review in reviews[:2]]
    conflicting = [reviews[2].review_id] if len(reviews) > 2 else []

    finding = {
        "title": "续费日期在购买确认前不可见",
        "problem_statement": "用户在确认订阅前看不到续费日期与最终价格，扣费后才知晓。",
        "topic_label": "订阅透明度",
        "topic_key": "subscription_transparency",
        "supporting_review_ids": supporting,
        "conflicting_review_ids": conflicting,
        "reasoning_summary": "多条评论直接描述续费日期与价格在确认步骤前不可见。",
        "limitations": ["样例仅覆盖部分区域与语言。"],
    }
    batch_analysis = {
        "findings": [finding],
        "review_summaries": [
            {
                "review_id": review.review_id,
                "summary_zh": "用户描述了订阅与训练体验方面的具体问题。",
            }
            for review in reviews
        ],
        "batch_limitations": [],
    }
    batches = make_review_batches(
        reviews,
        max_reviews=settings.batch_review_limit,
        max_characters=settings.batch_max_characters,
    )
    consolidation = {"findings": [finding]}
    evidence_audit = {
        "findings": [
            {
                "finding_index": 0,
                "assessments": [
                    {
                        "review_id": review_id,
                        "role": "supporting",
                        "rationale_zh": "原文直接说明续费日期或价格在确认前不可见。",
                    }
                    for review_id in supporting
                ]
                + [
                    {
                        "review_id": review_id,
                        "role": "conflicting",
                        "rationale_zh": "该评论认为订阅条款清晰，与上述问题相反。",
                    }
                    for review_id in conflicting
                ],
                "limitations": [],
            }
        ]
    }
    requirement_plan = {
        "requirements": [
            {
                "finding_ids": ["F-001"],
                "title": "订阅确认前明确展示续费日期与最终价格",
                "user_problem": "用户在确认购买前看不到续费日期与最终价格。",
                "objective": "在订阅确认步骤前可见地展示续费日期、试用期结束时间与最终价格。",
                "scope": ["订阅确认页", "定价说明文案"],
                "non_goals": ["不调整既有定价策略"],
                "functional_rules": ["确认前展示续费日期", "试用期结束前提醒"],
                "edge_cases": ["地区定价差异", "促销期价格变更"],
                "acceptance_criteria": ["确认页可见续费日期与最终价格"],
                "success_metrics": ["减少与续费相关的用户咨询"],
                "impact": 5,
                "complexity": "medium",
                "proposed_version": "V1.0",
                "assumptions": [],
            }
        ]
    }
    test_cases = {
        "test_cases": [
            {
                "requirement_id": "REQ-001",
                "title": "确认页展示续费日期与价格",
                "preconditions": ["用户已进入订阅流程"],
                "steps": ["打开订阅确认页", "查看续费日期与最终价格"],
                "expected_result": "确认前可见续费日期与最终价格",
                "case_type": "normal",
            },
            {
                "requirement_id": "REQ-001",
                "title": "试用期结束前提醒续费",
                "preconditions": ["用户处于试用期最后一天"],
                "steps": ["等待试用期结束前提醒", "查看提醒中的续费日期"],
                "expected_result": "提醒中包含准确的续费日期",
                "case_type": "boundary",
            },
            {
                "requirement_id": "REQ-001",
                "title": "价格获取失败时给出明确说明",
                "preconditions": ["定价服务不可用"],
                "steps": ["打开订阅确认页"],
                "expected_result": "页面明确说明价格暂不可用且不完成扣费",
                "case_type": "exception",
            },
        ]
    }
    return [
        *([batch_analysis] * len(batches)),
        consolidation,
        evidence_audit,
        requirement_plan,
        test_cases,
    ]


def _recording_services(settings: Settings, *, input_fingerprint: str):
    """与 factory 的 provider 段同构，只是把最内层换成预置响应的替身。"""
    from app_review_insights.collectors import AppStoreCollector
    from app_review_insights.llm.recording import RecordingProvider
    from app_review_insights.pipeline.analyze import (
        analyze_batch,
        audit_finding_evidence,
        consolidate_findings,
    )
    from app_review_insights.pipeline.orchestrator import PipelineServices
    from app_review_insights.pipeline.planning import build_requirements
    from app_review_insights.pipeline.test_generation import generate_test_cases
    from app_review_insights.pipeline.traceability import validate_traceability
    from app_review_insights.pipeline.validate import validate_finding_drafts
    from app_review_insights.storage import RunRepository
    from tests.conftest import SchemaFakeProvider

    provider = RecordingProvider(
        SchemaFakeProvider(_fake_responses()),
        settings.model_record_path,
        input_fingerprint=input_fingerprint,
        model="stub",
    )
    return PipelineServices(
        repository=RunRepository(settings.database_path),
        collector=AppStoreCollector(),
        finding_validator=validate_finding_drafts,
        traceability_validator=validate_traceability,
        batch_size=settings.batch_review_limit,
        batch_max_characters=settings.batch_max_characters,
        batch_analyzer=lambda reviews, goal: analyze_batch(provider, reviews, goal),
        consolidator=lambda results, goal, reviews: consolidate_findings(
            provider, results, goal, reviews
        ),
        evidence_auditor=lambda findings, reviews, goal: audit_finding_evidence(
            provider, findings, reviews, goal
        ),
        requirement_builder=lambda findings, goal, total: build_requirements(
            provider, findings, goal, total
        ),
        test_case_builder=lambda requirements: generate_test_cases(provider, requirements),
    )
