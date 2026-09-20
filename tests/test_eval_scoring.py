"""评测打分：主题比对必须走语言无关的 topic_key。

背景（2026-09-20 发现）：打分取的是 `finding.topic_label`——按 prompt 的要求它是**中文**标签，
而黄金集里是 ascii 主题键。`_normalize_topic` 会把非 ascii 字符整体替换成下划线，中文标签于是
规范化成空串、再被 `- {""}` 丢掉：`predicted_topics` 恒为空集，**topic_recall 结构性恒为 0**。
而 docs/model-and-prompts.md 与 AGENTS.md 都写着"评测只比对 topic_key"——那次修复只改了
Schema 与 Prompt，没落到打分代码里。

这组用例同时守住"覆盖率缺口可见"：模型只给中文键（规范化后为空）时，必须报出
`topic_key_coverage`，而不是安静地算成 0 分。
"""

from __future__ import annotations

import pytest

from scripts.run_eval import evaluate_case

CASE = {
    "case_id": "eval-scoring-fixture",
    "analysis_goal": "分析订阅与续费透明度",
    "expected_topics": ["subscription_transparency"],
    "expected_review_ids": ["r1"],
    "reviews": [
        {
            "review_id": "r1",
            "content": "The renewal price was hidden until the last screen.",
            "rating": 2,
            "language": "en",
            "published_at": "2026-06-01T10:00:00Z",
        }
    ],
}


def _batch(topic_key: str, *, review_ids: list[str] | None = None) -> dict:
    """模型按 prompt 要求返回：中文 topic_label + 稳定 ascii topic_key。"""
    return {
        "findings": [
            {
                "title": "续费价格不透明",
                "problem_statement": "用户直到最后一屏才看到续费价格。",
                "topic_label": "订阅透明度",
                "topic_key": topic_key,
                "supporting_review_ids": review_ids or ["r1"],
                "conflicting_review_ids": [],
                "reasoning_summary": "原文直接说明价格未提前披露。",
                "limitations": [],
            }
        ],
        "review_summaries": [{"review_id": "r1", "summary_zh": "续费价格直到最后才显示。"}],
        "batch_limitations": [],
    }


def test_topic_recall_scores_by_topic_key_not_by_chinese_label(fake_provider_factory):
    provider = fake_provider_factory([_batch("subscription_transparency")])

    result = evaluate_case(provider, CASE)

    assert result["topic_recall"] == 1.0, (
        "中文 topic_label 与黄金集的 ascii 键不可比；打分必须取 topic_key，"
        f"否则主题召回结构性恒为 0（实际 {result['topic_recall']}）"
    )
    assert result["topic_key_coverage"] == 1.0


def test_missing_topic_key_is_reported_as_coverage_gap(fake_provider_factory):
    """模型只给中文键（规范化后为空）时，缺口必须可见，而不是安静地记 0 分。"""
    provider = fake_provider_factory([_batch("")])

    result = evaluate_case(provider, CASE)

    assert result["topic_recall"] == 0.0
    assert result["topic_key_coverage"] == 0.0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Subscription Transparency", "subscription_transparency"),
        ("subscription-transparency", "subscription_transparency"),
        ("  订阅透明度  ", ""),
        ("订阅 transparency", "transparency"),
    ],
)
def test_topic_normalization_is_shared_with_the_schema(raw, expected):
    """比对前的规范化必须与 Schema 用**同一个**函数，避免两份实现悄悄分叉。"""
    from scripts.run_eval import _normalize_topic

    assert _normalize_topic(raw) == expected


def test_every_metric_computed_by_score_predictions_surfaces_in_the_case_result(
    fake_provider_factory,
):
    """结构性守卫：算了的指标必须出现在用例结果里。

    `evaluate_case` 是显式列字段返回的，漏列一个指标不会报错——它只会在汇总里
    变成一个看起来合理的 0.0（2026-09-20 就是 reference_recall 被这样丢掉的）。
    """
    from scripts.run_eval import score_predictions

    provider = fake_provider_factory([_batch("subscription_transparency")])

    result = evaluate_case(provider, CASE)
    scored = score_predictions(set(), set(), set(), set())

    missing = sorted(set(scored) - set(result))
    assert not missing, f"这些指标算了却没进用例结果（汇总会把它们静默当成 0）: {missing}"


def test_case_metrics_constant_covers_every_scored_metric():
    """`CASE_METRICS` 是逐用例投影与汇总共用的清单，必须盖住 `score_predictions` 的全部输出。"""
    from scripts.run_eval import CASE_METRICS, score_predictions

    assert set(score_predictions(set(), set(), set(), set())) <= set(CASE_METRICS)


def test_reference_recall_and_precision_constrain_each_other(fake_provider_factory):
    """召回与精确率必须能同时非零——这两个数在数学上互相约束。

    2026-09-20 的教训：汇总里出现过 precision=0.798 而 recall=0.0 的组合。那不是
    "模型一条标注评论都没覆盖"，而是指标被丢掉了。把不可能的组合挡在门外。
    """
    provider = fake_provider_factory([_batch("subscription_transparency", review_ids=["r1"])])
    case = {**CASE, "expected_review_ids": ["r1", "r2"]}

    result = evaluate_case(provider, case)

    assert result["reference_precision"] == 1.0, "模型引用的一条在标注里"
    assert result["reference_recall"] == 0.5, "标注两条，模型覆盖了一条"
