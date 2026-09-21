"""RAG 回答器：检索 → 生成（带引用）→ 确定性引用校验。"""

from __future__ import annotations

import unicodedata
from typing import Any

from app_review_insights.rag.retrieval import CorpusRetriever, RetrievedChunk
from app_review_insights.rag.rewriter import QueryRewriter
from app_review_insights.rag.schemas import Citation, RagAnswer

_RAG_SYSTEM_PROMPT = (
    "你是产品情报问答助手。基于给定评论证据回答，规则：\n"
    "1. 回答必须使用中文。\n"
    "2. 每条结论必须引用证据评论：citations 里给出 review_id 和原文片段 quote。\n"
    "3. 证据不足时 evidence_sufficient=false，并在 limitation 说明缺什么。\n"
    "4. 跨 App 对比时先分别总结再比较。"
)


def _quote_valid(quote: str, content: str) -> bool:
    """NFKC 归一化 + 空白折叠后，quote 必须是 chunk 原文的子串。

    NFKC 统一全角/半角、兼容字符与普通字符（如 "Ａ" → "A"、"，" → ","），
    使不同输入法下的等价引用都能通过校验。
    """

    def _normalize(text: str) -> str:
        return "".join(unicodedata.normalize("NFKC", text).split())

    quote_norm = _normalize(quote)
    content_norm = _normalize(content)
    return bool(quote_norm) and quote_norm in content_norm


def _render_chunks(chunks: list[RetrievedChunk]) -> str:
    lines = []
    for index, chunk in enumerate(chunks, start=1):
        lines.append(
            f"[{index}] review_id={chunk.review_id} app={chunk.app_id} "
            f"platform={chunk.platform}\n{chunk.content}"
        )
    return "\n".join(lines)


def gather_evidence(
    retriever: CorpusRetriever,
    question: str,
    app_ids: list[str],
    *,
    top_k: int = 10,
    rewriter: QueryRewriter | None = None,
) -> list[RetrievedChunk]:
    """检索策略：**先原查询，命中即止；只有零结果时才启用改写**（D-18）。

    这里是唯一的策略入口——`RagAnswerer` 与 `scripts/run_retrieval_eval.py` 共用它，
    所以评测量到的就是上线跑的那条路，而不是另抄一份。

    为什么要有这道闸：检索分是按**查询内** min-max 归一化的，每条查询的 top1 都是 1.0，
    跨查询比较这个分并不成立。无条件多路合并会让改写出的短查询（更泛）把原查询的精确
    结果挤下去——21 条查询实测 recall@3 0.595 → 0.524、MRR 0.873 → 0.746（D-18）。
    命中即止让这个负收益在**结构上**不可能发生：改写只在原查询零结果时上场，此时它
    只可能「从无到有」。

    回退路的合并按**名次**轮转（rank round-robin），而不是把所有结果堆在一起比分——
    分数跨查询不可比，那正是上面那个缺陷的根因。
    """
    hits = retriever.search_many(question, app_ids, top_k=top_k)
    if hits or rewriter is None:
        return sorted(hits, key=lambda chunk: chunk.score, reverse=True)[:top_k]
    variants = [item.strip() for item in rewriter.rewrite(question) if item.strip()]
    # 去重保序，并剔掉与原始问题重复的那条（它刚刚检索过且零结果）
    rounds = [
        retriever.search_many(variant, app_ids, top_k=top_k)
        for variant in dict.fromkeys(variants)
        if variant != question
    ]
    merged: list[RetrievedChunk] = []
    seen: set[str] = set()
    for rank in range(top_k):
        for round_hits in rounds:
            if rank >= len(round_hits):
                continue
            chunk = round_hits[rank]
            if chunk.review_id in seen:
                continue
            seen.add(chunk.review_id)
            merged.append(chunk)
    return merged[:top_k]


class RagAnswerer:
    def __init__(
        self,
        provider: Any | None,
        retriever: CorpusRetriever,
        top_k: int = 10,
        rewriter: QueryRewriter | None = None,
    ):
        self.provider = provider
        self.retriever = retriever
        self.top_k = top_k
        self.rewriter = rewriter or QueryRewriter()

    def answer(self, question: str, app_ids: list[str]) -> RagAnswer:
        # 检索策略（含改写的触发条件）在 gather_evidence 里，评测脚本共用同一入口。
        chunks = gather_evidence(
            self.retriever, question, app_ids, top_k=self.top_k, rewriter=self.rewriter
        )
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
        content_by_id = {chunk.review_id: chunk.content for chunk in chunks}
        citations: list[Citation] = []
        seen_review_ids: set[str] = set()
        for c in raw.citations:
            if (
                c.review_id in valid_ids
                and c.review_id not in seen_review_ids
                and _quote_valid(c.quote, content_by_id[c.review_id])
            ):
                seen_review_ids.add(c.review_id)
                citations.append(Citation(review_id=c.review_id, quote=c.quote))
        if citations:
            return RagAnswer(
                answer=raw.answer,
                citations=citations,
                evidence_sufficient=True,
                limitation="",
            )
        if raw.citations:
            # 模型给了引用但全部未通过校验
            return RagAnswer(
                answer=raw.answer,
                citations=[],
                evidence_sufficient=False,
                limitation="模型引用未通过校验，视为证据不足",
            )
        # 模型诚实报告证据不足：保留其 limitation
        return RagAnswer(
            answer=raw.answer,
            citations=[],
            evidence_sufficient=False,
            limitation=raw.limitation or "模型未提供证据引用",
        )
