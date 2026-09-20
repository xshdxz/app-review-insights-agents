"""幻觉率与稳定性：两个指标各有明确口径，不能互相冒充。

- 幻觉率比的是"引用的评论**到底存不存在**"——事实，不需要任何人工标注；
  与 reference_precision（比的是"与黄金标注是否一致"）不是一回事。
- 稳定性比的是"同输入多次运行是否识别出同一批主题"——只跑一次时**不谈稳定性**，
  报告里给 None 而不是 1.0，免得出现一个没测过的"稳定"。

夹具自包含（不从其他测试模块导入）：测试模块之间的相互依赖会让"改一个挂一片"。
"""

from __future__ import annotations

import pytest

from scripts.run_eval import evaluate_case, topic_set_stability

CASE = {
    "case_id": "eval-stability-fixture",
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


def test_hallucination_rate_counts_references_that_do_not_exist(fake_provider_factory):
    provider = fake_provider_factory(
        [_batch("subscription_transparency", review_ids=["r1", "invented"])]
    )

    result = evaluate_case(provider, CASE)

    assert result["hallucinated_review_ids"] == ["invented"]
    assert result["hallucination_rate"] == 0.5, "2 条引用里 1 条不存在"


def test_hallucination_rate_is_zero_when_nothing_is_predicted(fake_provider_factory):
    """没有任何引用时不能除零。"""
    provider = fake_provider_factory(
        [{"findings": [], "review_summaries": [], "batch_limitations": []}]
    )

    result = evaluate_case(provider, CASE)

    assert result["hallucination_rate"] == 0.0
    assert result["predicted_review_ids"] == []


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ({"a", "b"}, {"a", "b"}, 1.0),
        ({"a"}, {"b"}, 0.0),
        ({"a", "b"}, {"b", "c"}, 1 / 3),
    ],
)
def test_topic_set_stability_is_pairwise_jaccard(left, right, expected):
    assert topic_set_stability([left, right]) == pytest.approx(round(expected, 3))


def test_topic_set_stability_of_a_single_run_is_one():
    """单次运行的"稳定"是平凡值；调用方要靠 runs 字段判断它有没有被真的测过。"""
    assert topic_set_stability([{"a"}]) == 1.0


def test_repeats_average_the_metrics_and_report_stability(fake_provider_factory):
    provider = fake_provider_factory(
        [
            _batch("subscription_transparency"),
            _batch("pricing_clarity"),
        ]
    )

    result = evaluate_case(provider, CASE, repeats=2)

    assert result["runs"] == 2
    assert result["stability"] == 0.0, "两次跑出完全不同的主题集合 ⇒ 一致度为 0"
    assert result["topic_recall"] == 0.5, "一次命中、一次未命中 ⇒ 取均值"


def test_single_run_reports_no_stability(fake_provider_factory):
    provider = fake_provider_factory([_batch("subscription_transparency")])

    result = evaluate_case(provider, CASE)

    assert result["runs"] == 1
    assert result["stability"] is None, "跑一次就没资格谈稳定性"


def test_repeats_must_be_positive(fake_provider_factory):
    provider = fake_provider_factory([_batch("subscription_transparency")])

    with pytest.raises(ValueError):
        evaluate_case(provider, CASE, repeats=0)
