"""查询改写器：把用户自然语言问题扩展为多条等价检索表达，提升 RAG 召回率。

改写失败时静默回退为原始查询（降级不崩溃）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

_REWRITE_SYSTEM_PROMPT = (
    "你是一个检索优化助手。用户会给你一个产品评论分析的问题。\n"
    "请把这个问题改写为 3-5 个等价的短搜索词组（不超过 8 个字），覆盖问题的不同角度。\n"
    "规则：\n"
    "1. 只输出 JSON：{\"queries\": [\"词组1\", \"词组2\", ...]}\n"
    "2. 词组要适合全文搜索（短、具体、有区分度），不要输出完整句子\n"
    "3. 每个词组用和原始问题相同的语言（中文用中文，英文用英文）\n"
    "4. 覆盖同义表达：如「订阅」可扩展为「续费」「自动扣费」「价格」等\n"
    "5. 第一个词组应该是对原始问题最直接的简化"
)


class RewrittenQueries(BaseModel):
    queries: list[str] = Field(min_length=1, max_length=10)


def _render_rewrite_prompt(question: str) -> str:
    return f"问题：{question}\n\n请改写为适合评论检索的短词组（JSON 格式）。"


class QueryRewriter:
    """把自然语言问题扩展为多条等价检索表达。

    provider 为 None 或调用失败时，直接返回原始问题（单元素列表）。
    """

    def __init__(self, provider: Any | None = None):
        self.provider = provider

    def rewrite(self, question: str) -> list[str]:
        """返回 3-5 条等价短查询；失败时回退为原始问题。"""
        if self.provider is None:
            return [question]
        try:
            result = self.provider.generate(
                _REWRITE_SYSTEM_PROMPT,
                _render_rewrite_prompt(question),
                RewrittenQueries,
            )
            queries = [q.strip() for q in result.queries if q.strip()]
            if not queries:
                return [question]
            # 去重但保序
            seen: set[str] = set()
            unique: list[str] = []
            for q in queries:
                if q.lower() not in seen:
                    seen.add(q.lower())
                    unique.append(q)
            return unique or [question]
        except Exception:  # noqa: BLE001 - 改写失败降级为原始查询
            return [question]
