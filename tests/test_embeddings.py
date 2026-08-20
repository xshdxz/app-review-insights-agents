import pytest

from app_review_insights.rag.embeddings import cosine_similarity
from app_review_insights.storage.agent_repository import AgentRepository


@pytest.fixture
def repo(tmp_path):
    return AgentRepository(tmp_path / "agent.sqlite3")


def test_cosine_similarity_identical():
    vector = [1.0, 2.0, 3.0]
    assert cosine_similarity(vector, vector) == pytest.approx(1.0)


def test_cosine_similarity_orthogonal():
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_zero_vector():
    assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0


def test_cosine_similarity_dimension_mismatch_raises():
    with pytest.raises(ValueError):
        cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0])


def test_embedding_upsert_and_query(repo):
    from datetime import UTC, datetime

    from app_review_insights.models import Review

    def _review(review_id, content):
        return Review(
            review_id=review_id,
            app_id="app-a",
            content_original=content,
            rating=3,
            published_at=datetime(2026, 1, 1, tzinfo=UTC),
            source="fixture",
        )

    # embeddings 与 corpus JOIN：必须先写入语料
    repo.upsert_corpus(_review("v1", "订阅太贵了"))
    repo.upsert_corpus(_review("v2", "界面很漂亮"))
    repo.upsert_embedding("v1", [1.0, 0.0, 0.0])
    repo.upsert_embedding("v2", [0.0, 1.0, 0.0])
    hits = repo.search_embeddings([1.0, 0.0, 0.0], app_ids=None, limit=2)
    assert hits[0]["review_id"] == "v1"
    assert hits[0]["score"] == pytest.approx(1.0)
