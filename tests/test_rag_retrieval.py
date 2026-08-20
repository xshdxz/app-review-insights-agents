from datetime import UTC, datetime

import pytest

from app_review_insights.models import Review
from app_review_insights.rag.indexer import CorpusIndexer
from app_review_insights.rag.retrieval import CorpusRetriever
from app_review_insights.storage.agent_repository import AgentRepository


@pytest.fixture
def repo(tmp_path):
    return AgentRepository(tmp_path / "agent.sqlite3")


def _review(review_id, app_id, content, platform="app-store"):
    return Review(
        review_id=review_id,
        app_id=app_id,
        content_original=content,
        rating=3,
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
        source="fixture",
        platform=platform,
    )


def test_index_reviews_and_app_ids(repo):
    indexer = CorpusIndexer(repo)
    reviews = [
        _review("v1", "app-a", "订阅太贵了"),
        _review("v2", "app-a", "界面很漂亮"),
        _review("v3", "app-b", "订阅流程顺畅"),
    ]
    assert indexer.index_reviews(reviews) == 3
    assert set(repo.app_ids()) == {"app-a", "app-b"}


def test_retriever_single_app(repo):
    indexer = CorpusIndexer(repo)
    indexer.index_reviews(
        [
            _review("v1", "app-a", "订阅太贵了，续费不划算"),
            _review("v2", "app-a", "界面很漂亮"),
            _review("v3", "app-b", "订阅流程顺畅"),
        ]
    )
    retriever = CorpusRetriever(repo)
    hits = retriever.search("订阅", app_ids=["app-a"], top_k=5)
    assert {h.review_id for h in hits} == {"v1"}
    assert hits[0].app_id == "app-a"


def test_retriever_cross_app_merge(repo):
    indexer = CorpusIndexer(repo)
    indexer.index_reviews(
        [
            _review("v1", "app-a", "订阅太贵了"),
            _review("v2", "app-b", "订阅流程顺畅"),
        ]
    )
    retriever = CorpusRetriever(repo)
    hits = retriever.search_many("订阅", ["app-a", "app-b"], top_k=5)
    assert {h.review_id for h in hits} == {"v1", "v2"}
    assert {h.app_id for h in hits} == {"app-a", "app-b"}


def test_retriever_filters_social_by_default(repo):
    indexer = CorpusIndexer(repo)
    indexer.index_reviews(
        [
            _review("v1", "app-a", "订阅太贵了"),
            _review("s1", "app-a", "有人在说订阅贵", platform="social"),
        ]
    )
    retriever = CorpusRetriever(repo)
    hits = retriever.search("订阅", app_ids=["app-a"], top_k=5)
    assert {h.review_id for h in hits} == {"v1"}


def test_retriever_english_query(repo):
    indexer = CorpusIndexer(repo)
    indexer.index_reviews([_review("v1", "app-a", "Great subscription value")])
    retriever = CorpusRetriever(repo)
    hits = retriever.search("subscription", app_ids=["app-a"], top_k=5)
    assert hits[0].review_id == "v1"
