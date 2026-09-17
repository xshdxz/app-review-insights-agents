"""语言无关的主题键 topic_key。

评测一直拿模型生成的**中文标签**（topic_label）与黄金集做精确字符串比对，
所以 topic_recall 长期是 0.000——那不是模型没识别出主题，是**指标本身不可比**。

topic_key 是模型额外输出的稳定 ascii snake_case 键：展示仍用本地化的
topic_label，比对只用 topic_key。
"""

from __future__ import annotations

import pytest

from app_review_insights.llm.schemas import FindingDraft, normalize_topic_key


def _draft(**overrides) -> FindingDraft:
    payload = {
        "title": "免费内容减少",
        "problem_statement": "更新后免费功能被大幅削减",
        "topic_label": "免费内容与付费墙",
        "topic_key": "free_content_paywall",
        "supporting_review_ids": ["r-1"],
        "reasoning_summary": "多条评论提到同一问题",
    }
    payload.update(overrides)
    return FindingDraft(**payload)


# ── 规范化 ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("subscription_transparency", "subscription_transparency"),
        ("Subscription Transparency", "subscription_transparency"),
        ("Subscription-Transparency", "subscription_transparency"),
        ("  Subscription  Transparency  ", "subscription_transparency"),
        ("Subscription/Transparency", "subscription_transparency"),
        ("subscription.transparency", "subscription_transparency"),
        ("SUBSCRIPTION_TRANSPARENCY", "subscription_transparency"),
    ],
)
def test_normalize_topic_key_produces_stable_snake_case(raw, expected):
    assert normalize_topic_key(raw) == expected


def test_normalize_topic_key_drops_non_ascii():
    """纯中文键不可比对，宁可留空也不要制造一个永远匹配不上的值。"""
    assert normalize_topic_key("订阅透明度") == ""


def test_normalize_topic_key_keeps_ascii_part_of_mixed_input():
    assert normalize_topic_key("订阅 subscription") == "subscription"


def test_normalize_topic_key_handles_empty_and_punctuation_only():
    assert normalize_topic_key("") == ""
    assert normalize_topic_key("---") == ""


# ── schema 行为 ──────────────────────────────────────────────────────────────


def test_finding_draft_normalizes_key_on_validation():
    assert _draft(topic_key="Free Content Paywall").topic_key == "free_content_paywall"


def test_finding_draft_key_defaults_to_empty_for_backward_compatibility():
    """旧夹具与旧模型输出不该因为新增字段而整体失败。"""
    draft = FindingDraft(
        title="t",
        problem_statement="p",
        topic_label="标签",
        supporting_review_ids=["r-1"],
        reasoning_summary="s",
    )
    assert draft.topic_key == ""


def test_finding_draft_keeps_label_untouched():
    """展示仍用本地化标签，规范化只作用于 key。"""
    assert _draft().topic_label == "免费内容与付费墙"


def test_finding_model_carries_topic_key():
    from app_review_insights.models import EvidenceStatus, Finding

    finding = Finding(
        finding_id="F-001",
        title="t",
        problem_statement="p",
        topic_label="标签",
        topic_key="free_content_paywall",
        supporting_review_ids=["r-1"],
        confidence=0.8,
        evidence_status=EvidenceStatus.VALIDATED,
        model_reasoning_summary="s",
    )
    assert finding.topic_key == "free_content_paywall"


# ── 评测打分 ─────────────────────────────────────────────────────────────────


def test_scoring_uses_topic_keys_when_available():
    from scripts.run_eval import score_predictions

    score = score_predictions(
        expected_topics={"subscription_transparency"},
        expected_review_ids={"r-1"},
        predicted_topics={"subscription_transparency"},
        predicted_review_ids={"r-1"},
    )

    assert score["topic_recall"] == 1.0
    assert score["reference_precision"] == 1.0


def test_scoring_is_case_and_separator_insensitive():
    from scripts.run_eval import score_predictions

    score = score_predictions(
        expected_topics={"Subscription Transparency"},
        expected_review_ids=set(),
        predicted_topics={"subscription_transparency"},
        predicted_review_ids=set(),
    )

    assert score["topic_recall"] == 1.0
