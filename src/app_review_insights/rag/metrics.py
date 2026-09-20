"""检索指标：recall@k / MRR / precision@k。

为什么自己写而不是引评测库：这三个指标各自不到十行，而引入 `ranx` 之类还要适配它们的输入格式
（run/qrels 文件），换来的只是同一批除法。**真正容易出错的地方不在实现，而在口径**：

- recall 的分母是「期望项的个数」，不是「返回了多少条」；
- precision 的分母是「实际返回的条数」，不是 k；
- MRR 只看**第一条**命中，后面的命中不再加分；
- 期望为空时 recall 记 1.0（没有东西要找，不该扣分）；precision 则记 0.0（负向查询里
  返回了东西就是错）。

这几个约定写进 docstring 与单测，是因为这个项目已经栽过两次「指标本身错了」：
D-10（打分取了不可比的字段，头条指标结构性恒为 0）与 D-11（用词面一致率冒充召回）。
"""

from __future__ import annotations

from collections.abc import Collection, Sequence


def _top(retrieved: Sequence[str], k: int) -> list[str]:
    """前 k 条结果。k 必须是正整数——k=0 会让所有指标变成 1.0/0.0 的假象，宁可报错。"""
    if k < 1:
        raise ValueError(f"截断位置 k 必须是正整数，收到 {k}")
    return list(retrieved)[:k]


def recall_at_k(expected: Collection[str], retrieved: Sequence[str], *, k: int) -> float:
    """前 k 条里找回了多少比例的**期望项**。

    期望为空 ⇒ 1.0：没有东西要找就不该扣分（负向查询该看 precision）。
    """
    wanted = set(expected)
    if not wanted:
        return 1.0
    return len(wanted & set(_top(retrieved, k))) / len(wanted)


def reciprocal_rank(expected: Collection[str], retrieved: Sequence[str]) -> float:
    """第一条命中的排名的倒数（1/rank）；一条都没命中 ⇒ 0.0。

    它衡量的是「最相关的那条被排到了多靠前」，比 recall 更敏感于排序质量。
    """
    wanted = set(expected)
    for rank, review_id in enumerate(retrieved, start=1):
        if review_id in wanted:
            return 1 / rank
    return 0.0


def precision_at_k(expected: Collection[str], retrieved: Sequence[str], *, k: int) -> float:
    """前 k 条里有多少是期望项。

    分母是**实际返回的条数**：只返回 2 条时，命中 1 条就是 50%，而不是 1/k。
    期望为空时，返回了任何东西都算错（0.0）；什么都没返回则记 1.0。
    """
    top = _top(retrieved, k)
    wanted = set(expected)
    if not top:
        return 1.0 if not wanted else 0.0
    return len(set(top) & wanted) / len(top)
