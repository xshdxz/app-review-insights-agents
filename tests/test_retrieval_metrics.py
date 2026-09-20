"""检索指标：recall@k / MRR / precision@k。

这些是纯函数——不碰数据库、不碰模型。它们值得单独存在并配单测，是因为**指标本身错了比没有指标更糟**：
数字会进文档、进 CI 门禁、进「混合检索更好」这类结论里（D-10/D-11 就是指标出错的两副面孔）。
"""

from __future__ import annotations

import pytest

from app_review_insights.rag.metrics import precision_at_k, recall_at_k, reciprocal_rank

# ── recall@k ─────────────────────────────────────────────────────────────────


def test_recall_counts_hits_within_the_cutoff():
    """只有前 k 条算数——第 4 名命中，在 k=3 的视角里就是没找到。"""
    expected = ["r1"]

    assert recall_at_k(expected, ["r2", "r3", "r4", "r1"], k=3) == 0.0
    assert recall_at_k(expected, ["r2", "r3", "r4", "r1"], k=4) == 1.0


def test_recall_is_a_fraction_of_all_expected_items():
    """召回是「找回了多少个期望项」的比例，不是「命中了几条」。"""
    expected = ["r1", "r2", "r3", "r4"]

    assert recall_at_k(expected, ["r1", "r2"], k=5) == 0.5


def test_recall_ignores_duplicate_ids_in_results():
    """同一条评论被返回两次不该算两次命中。"""
    assert recall_at_k(["r1", "r2"], ["r1", "r1", "r1"], k=5) == 0.5


def test_recall_with_no_expected_items_is_vacuously_perfect():
    """没有东西要找 ⇒ 不扣分。负向查询（什么都不该命中）该看 precision，不是 recall。"""
    assert recall_at_k([], ["r1"], k=5) == 1.0
    assert recall_at_k([], [], k=5) == 1.0


def test_recall_with_no_results_is_zero_when_something_was_expected():
    assert recall_at_k(["r1"], [], k=5) == 0.0


@pytest.mark.parametrize("k", [0, -1])
def test_recall_rejects_a_nonsense_cutoff(k):
    """k=0 会让所有召回都变成 1.0 或 0.0 的假象——这种输入应当直接报错，而不是静默给个数。"""
    with pytest.raises(ValueError):
        recall_at_k(["r1"], ["r1"], k=k)


# ── MRR ──────────────────────────────────────────────────────────────────────


def test_reciprocal_rank_rewards_early_hits():
    assert reciprocal_rank(["r1"], ["r1"]) == 1.0
    assert reciprocal_rank(["r1"], ["r9", "r1"]) == 0.5
    assert reciprocal_rank(["r1"], ["r9", "r8", "r1"]) == pytest.approx(1 / 3)


def test_reciprocal_rank_is_zero_without_a_hit():
    assert reciprocal_rank(["r1"], ["r9", "r8"]) == 0.0
    assert reciprocal_rank(["r1"], []) == 0.0


def test_reciprocal_rank_uses_the_first_hit_only():
    """MRR 关心的是「第一条命中有多靠前」，后面的命中不再加分。"""
    assert reciprocal_rank(["r1", "r2"], ["r2", "r1"]) == 1.0


# ── precision@k ──────────────────────────────────────────────────────────────


def test_precision_is_measured_against_what_was_returned():
    """分母是**返回了多少条**而不是 k：只返回 2 条时，1 条命中就是 50%。"""
    assert precision_at_k(["r1"], ["r1", "r9"], k=5) == 0.5


def test_precision_with_an_empty_expectation_punishes_any_hit():
    """负向查询：返回了东西就是错。"""
    assert precision_at_k([], ["r9"], k=5) == 0.0
    assert precision_at_k([], [], k=5) == 1.0


def test_precision_respects_the_cutoff():
    assert precision_at_k(["r1"], ["r9", "r8", "r1"], k=2) == 0.0
