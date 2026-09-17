"""评测集完整性与覆盖率。

评测集本身坏掉是最隐蔽的失败：跑出来的数字毫无意义，却没人发现。
这些断言让数据集的问题在 CI 里当场暴露，而不是等到看指标时才起疑。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app_review_insights.llm.schemas import normalize_topic_key
from scripts.run_eval import validate_dataset

DATASET = Path(__file__).parents[1] / "evals" / "gold-reviews.json"


@pytest.fixture(scope="module")
def gold() -> dict:
    return json.loads(DATASET.read_text(encoding="utf-8"))


# ── 真实数据集必须干净 ───────────────────────────────────────────────────────


def test_gold_dataset_has_no_integrity_problems(gold):
    assert validate_dataset(gold) == []


def test_dataset_is_large_enough_to_say_anything(gold):
    """3 个用例只能当回归信号，不能作为模型质量的结论。"""
    assert len(gold["cases"]) >= 30


def test_dataset_covers_multiple_languages(gold):
    languages = {
        review.get("language", "en") for case in gold["cases"] for review in case["reviews"]
    }
    assert {"zh", "en"} <= languages


def test_dataset_contains_mixed_language_cases(gold):
    mixed = [
        case
        for case in gold["cases"]
        if {review.get("language", "en") for review in case["reviews"]} == {"zh", "en"}
    ]
    assert mixed, "至少要有中英混排的用例"


def test_dataset_covers_documented_failure_modes(gold):
    """文档里点名的缺口都要有用例，否则回归信号是假的。"""
    ids = {case["case_id"] for case in gold["cases"]}

    assert any("conflicting" in case_id for case_id in ids), "缺少冲突证据用例"
    assert any("insufficient" in case_id for case_id in ids), "缺少证据不足用例"
    assert any("injection" in case_id for case_id in ids), "缺少 prompt 注入用例"
    assert any("duplicate" in case_id for case_id in ids), "缺少重复评论用例"
    assert any("all-positive" in case_id for case_id in ids), "缺少全正面用例"


def test_dataset_has_cases_with_no_expected_topics(gold):
    """「证据不足」场景必须真的存在，否则等于没有测到它。"""
    assert any(not case["expected_topics"] for case in gold["cases"])


# ── 校验器能抓到问题 ─────────────────────────────────────────────────────────


def _minimal_case(**overrides) -> dict:
    case = {
        "case_id": "eval-x",
        "analysis_goal": "分析订阅转化",
        "expected_topics": ["subscription_transparency"],
        "expected_review_ids": ["r-1"],
        "reviews": [{"review_id": "r-1", "content": "price hidden", "rating": 1}],
    }
    case.update(overrides)
    return case


def test_validate_accepts_a_clean_dataset():
    assert validate_dataset({"cases": [_minimal_case()]}) == []


def test_validate_rejects_duplicate_case_ids():
    problems = validate_dataset({"cases": [_minimal_case(), _minimal_case()]})
    assert any("重复" in problem for problem in problems)


def test_validate_rejects_unknown_expected_review_id():
    problems = validate_dataset({"cases": [_minimal_case(expected_review_ids=["r-999"])]})
    assert any("r-999" in problem for problem in problems)


def test_validate_rejects_non_ascii_topic():
    """非 ascii 主题键不可比对，会让指标永远匹配不上。"""
    problems = validate_dataset({"cases": [_minimal_case(expected_topics=["订阅透明度"])]})
    assert any("主题键" in problem for problem in problems)


def test_validate_rejects_out_of_range_rating():
    problems = validate_dataset(
        {"cases": [_minimal_case(reviews=[{"review_id": "r-1", "content": "x", "rating": 9}])]}
    )
    assert any("rating" in problem for problem in problems)


def test_validate_rejects_duplicate_review_ids_within_a_case():
    problems = validate_dataset(
        {
            "cases": [
                _minimal_case(
                    reviews=[
                        {"review_id": "r-1", "content": "x", "rating": 1},
                        {"review_id": "r-1", "content": "y", "rating": 2},
                    ]
                )
            ]
        }
    )
    assert any("重复" in problem for problem in problems)


def test_validate_rejects_case_without_reviews():
    problems = validate_dataset({"cases": [_minimal_case(reviews=[])]})
    assert any("没有评论" in problem for problem in problems)


def test_validate_rejects_missing_top_level_key():
    assert validate_dataset({}) != []


# ── 与 topic_key 规范化保持一致 ──────────────────────────────────────────────


def test_expected_topic_keys_are_already_normalized(gold):
    """黄金集里的键必须是规范形态，否则又是「改了词就不会匹配」。"""
    for case in gold["cases"]:
        for topic in case["expected_topics"]:
            assert normalize_topic_key(topic) == topic, f"{case['case_id']} 的 {topic} 未规范化"
