"""语料索引器：把清洗后的评论写入 FTS5 语料库（幂等，可重复索引）。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app_review_insights.models import Review

if TYPE_CHECKING:
    from app_review_insights.storage.agent_repository import AgentRepository


class CorpusIndexer:
    def __init__(self, agent_repository: AgentRepository):
        self.agent_repository = agent_repository

    def index_reviews(self, reviews: list[Review]) -> int:
        for review in reviews:
            self.agent_repository.upsert_corpus(review)
        return len(reviews)

    def remove_app(self, app_id: str) -> int:
        return self.agent_repository.delete_app(app_id)

    def app_ids(self) -> list[str]:
        return self.agent_repository.app_ids()
