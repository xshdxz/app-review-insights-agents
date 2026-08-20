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


def test_retriever_include_social(repo):
    indexer = CorpusIndexer(repo)
    indexer.index_reviews(
        [
            _review("v1", "app-a", "订阅太贵了"),
            _review("s1", "app-a", "有人在说订阅贵", platform="social"),
        ]
    )
    retriever = CorpusRetriever(repo, include_social=True)
    hits = retriever.search("订阅", app_ids=["app-a"], top_k=5)
    assert {h.review_id for h in hits} == {"v1", "s1"}


def test_retriever_no_match_returns_empty(repo):
    indexer = CorpusIndexer(repo)
    indexer.index_reviews([_review("v1", "app-a", "订阅太贵了")])
    retriever = CorpusRetriever(repo)
    assert retriever.search("完全不存在的词", app_ids=["app-a"], top_k=5) == []


def test_retriever_top_k_truncation(repo):
    indexer = CorpusIndexer(repo)
    indexer.index_reviews(
        [
            _review("v1", "app-a", "订阅太贵了"),
            _review("v2", "app-a", "订阅服务流畅"),
            _review("v3", "app-a", "订阅价格合理"),
        ]
    )
    retriever = CorpusRetriever(repo)
    hits = retriever.search("订阅", app_ids=["app-a"], top_k=1)
    assert len(hits) == 1


def test_retriever_top_k_at_least_one(repo):
    indexer = CorpusIndexer(repo)
    indexer.index_reviews([_review("v1", "app-a", "订阅太贵了")])
    retriever = CorpusRetriever(repo)
    hits = retriever.search("订阅", app_ids=["app-a"], top_k=0)
    assert len(hits) == 1


class _FakeEmbeddingStore:
    def __init__(self, vector, fail=False):
        self._vector = vector
        self._fail = fail

    def embed_texts(self, texts):
        if self._fail:
            raise RuntimeError("embedding unavailable")
        return [self._vector for _ in texts]


def test_retriever_hybrid_blends_normalized_scores(repo):
    indexer = CorpusIndexer(repo)
    indexer.index_reviews(
        [
            _review("v1", "app-a", "订阅太贵了"),
            _review("v2", "app-a", "订阅太贵了"),
        ]
    )
    repo.upsert_embedding("v1", [0.0, 1.0])
    repo.upsert_embedding("v2", [1.0, 0.0])
    retriever = CorpusRetriever(repo, embedding_store=_FakeEmbeddingStore([1.0, 0.0]))
    hits = retriever.search("订阅", app_ids=["app-a"], top_k=5)
    assert hits[0].review_id == "v2"
    assert hits[1].review_id == "v1"
    # 相同 FTS 文档 → span==0 → 归一化到 1.0；混合后 v2=0.6*1.0+0.4*1.0，v1=0.6*1.0+0.4*0.0
    assert hits[0].score == pytest.approx(1.0)
    assert hits[1].score == pytest.approx(0.6)
    assert all(0.0 <= hit.score <= 1.0 for hit in hits)


def test_retriever_hybrid_falls_back_on_embedding_error(repo):
    indexer = CorpusIndexer(repo)
    indexer.index_reviews(
        [
            _review("v1", "app-a", "订阅太贵了，续费不划算，用户抱怨很多"),
            _review("v2", "app-a", "订阅服务很流畅"),
        ]
    )
    retriever = CorpusRetriever(repo, embedding_store=_FakeEmbeddingStore([1.0, 0.0], fail=True))
    hits = retriever.search("订阅", app_ids=["app-a"], top_k=5)
    assert {h.review_id for h in hits} == {"v1", "v2"}
    assert all(0.0 <= hit.score <= 1.0 for hit in hits)
