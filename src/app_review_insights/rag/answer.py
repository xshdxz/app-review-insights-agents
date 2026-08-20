"""RAG 回答器：检索 → 生成（带引用）→ 确定性引用校验。"""

from __future__ import annotations

from typing import Any

from app_review_insights.rag.retrieval import CorpusRetriever
from app_review_insights.rag.schemas import Citation, RagAnswer

_RAG_SYSTEM_PROMPT = (
    "你是产品情报问答助手。基于给定评论证据回答，规则：\n"
    "1. 回答必须使用中文。\n"
    "2. 每条结论必须引用证据评论：citations 里给出 review_id 和原文片段 quote。\n"
    "3. 证据不足时 evidence_sufficient=false，并在 limitation 说明缺什么。\n"
    "4. 跨 App 对比时先分别总结再比较。"
)


def _render_chunks(chunks) -> str:
    lines = []
    for index, chunk in enumerate(chunks, start=1):
        lines.append(
            f"[{index}] review_id={chunk.review_id} app={chunk.app_id} "
            f"platform={chunk.platform}\n{chunk.content}"
        )
    return "\n".join(lines)


class RagAnswerer:
    def __init__(
        self,
        provider: Any,
        retriever: CorpusRetriever,
        top_k: int = 10,
    ):
        self.provider = provider
        self.retriever = retriever
        self.top_k = top_k

    def answer(self, question: str, app_ids: list[str]) -> RagAnswer:
        chunks = self.retriever.search_many(question, app_ids, top_k=self.top_k)
        if not chunks:
            return RagAnswer(
                answer="当前语料中没有找到与该问题相关的评论。",
                citations=[],
                evidence_sufficient=False,
                limitation="语料为空或无匹配评论",
            )
        if self.provider is None:
            return RagAnswer(
                answer="模型未配置，无法生成回答；以下是与问题相关的评论：\n"
                + _render_chunks(chunks),
                citations=[
                    Citation(review_id=chunk.review_id, quote=chunk.content[:100])
                    for chunk in chunks[:5]
                ],
                evidence_sufficient=True,
                limitation="模型未配置，仅展示检索结果",
            )
        user_prompt = f"问题：{question}\n\n证据评论：\n{_render_chunks(chunks)}"
        try:
            raw = self.provider.generate(_RAG_SYSTEM_PROMPT, user_prompt, RagAnswer)
        except Exception:  # noqa: BLE001 - 模型故障返回明确 limitation 而非崩溃
            return RagAnswer(
                answer="模型暂时不可用，无法生成回答。",
                citations=[],
                evidence_sufficient=False,
                limitation="模型调用失败",
            )
        valid_ids = {chunk.review_id for chunk in chunks}
        citations = [
            Citation(review_id=c.review_id, quote=c.quote)
            for c in raw.citations
            if c.review_id in valid_ids
        ]
        return RagAnswer(
            answer=raw.answer,
            citations=citations,
            evidence_sufficient=bool(citations),
            limitation="" if citations else "模型引用未通过校验，视为证据不足",
        )
