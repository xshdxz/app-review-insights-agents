"""检索器：FTS5 BM25 为主，可选 embedding 混合；默认排除社交舆情语料。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

if TYPE_CHECKING:
    from app_review_insights.storage.agent_repository import AgentRepository

_REVIEW_PLATFORMS = {"app-store", "google-play"}


class RetrievedChunk(BaseModel):
    review_id: str
    app_id: str
    content: str
    # 检索分：纯 FTS 模式为原始 -bm25（越大越相关，可为负）；
    # 混合模式为 min-max 归一化到 [0,1] 后的加权分 0.6*fts + 0.4*vector，恒非负。
    score: float
    platform: str
    source: str
    storefront: str = "us"


class CorpusRetriever:
    def __init__(
        self,
        agent_repository: AgentRepository,
        embedding_store: Any | None = None,
        include_social: bool = False,
    ):
        self.agent_repository = agent_repository
        self.embedding_store = embedding_store
        self.include_social = include_social

    def search(
        self,
        query: str,
        app_ids: list[str] | None = None,
        top_k: int = 10,
    ) -> list[RetrievedChunk]:
        top_k = max(1, min(top_k, 100))
        fts_hits = self.agent_repository.search_corpus(query, app_ids=app_ids, limit=top_k * 3)
        chunks = [
            RetrievedChunk(
                review_id=hit["review_id"],
                app_id=hit["app_id"],
                content=hit["content"],
                score=float(hit["score"]),
                platform=hit.get("platform") or "app-store",
                source=hit.get("source") or "",
                storefront=hit.get("storefront") or "us",
            )
            for hit in fts_hits
        ]
        chunks = [chunk for chunk in chunks if self._allowed_platform(chunk.platform)]
        if not chunks:
            return []
        return self._rank(query, chunks, app_ids, top_k)

    def search_many(
        self,
        query: str,
        app_ids: list[str],
        top_k: int = 10,
    ) -> list[RetrievedChunk]:
        """跨 App：每个 App 独立取 top，再按分数合并去重（保证每个 App 都有代表）。"""
        top_k = max(1, min(top_k, 100))
        per_app: dict[str, RetrievedChunk] = {}
        for app_id in app_ids:
            for chunk in self.search(query, app_ids=[app_id], top_k=top_k):
                if chunk.review_id not in per_app:
                    per_app[chunk.review_id] = chunk
        merged = sorted(per_app.values(), key=lambda chunk: chunk.score, reverse=True)
        return merged[:top_k]

    def _allowed_platform(self, platform: str) -> bool:
        if self.include_social:
            return True
        return platform in _REVIEW_PLATFORMS

    def _rank(
        self,
        query: str,
        chunks: list[RetrievedChunk],
        app_ids: list[str] | None,
        top_k: int,
    ) -> list[RetrievedChunk]:
        # search_corpus 已将 -bm25 归一化到 [0,1]，可直接排序。
        if self.embedding_store is None:
            return sorted(chunks, key=lambda chunk: chunk.score, reverse=True)[:top_k]
        # 混合排序：FTS 分（已是 [0,1]）与 cosine [0,1] 加权融合。
        try:
            query_vector = self.embedding_store.embed_texts([query])[0]
            hits = self.agent_repository.search_embeddings(
                query_vector, app_ids=app_ids, limit=top_k * 3
            )
        except Exception:  # noqa: BLE001 - embedding 故障降级为纯 FTS
            return sorted(chunks, key=lambda chunk: chunk.score, reverse=True)[:top_k]
        by_id = {hit["review_id"]: float(hit["score"]) for hit in hits}
        for chunk in chunks:
            vector_score = by_id.get(chunk.review_id, 0.0)
            chunk.score = 0.6 * chunk.score + 0.4 * vector_score
        return sorted(chunks, key=lambda chunk: chunk.score, reverse=True)[:top_k]
