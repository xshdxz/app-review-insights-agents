from datetime import UTC, datetime

import pytest

from app_review_insights.errors import RecoverableModelError
from app_review_insights.models import Review
from app_review_insights.rag.answer import RagAnswerer
from app_review_insights.rag.indexer import CorpusIndexer
from app_review_insights.rag.retrieval import CorpusRetriever
from app_review_insights.rag.schemas import RagAnswer
from app_review_insights.storage.agent_repository import AgentRepository


@pytest.fixture
def repo(tmp_path):
    return AgentRepository(tmp_path / "agent.sqlite3")


@pytest.fixture
def retriever(repo):
    indexer = CorpusIndexer(repo)
    indexer.index_reviews(
        [
            Review(
                review_id="v1",
                app_id="app-a",
                content_original="订阅太贵了，续费不划算",
                rating=1,
                published_at=datetime(2026, 1, 1, tzinfo=UTC),
                source="fixture",
            ),
            Review(
                review_id="v2",
                app_id="app-a",
                content_original="界面很漂亮，但引导不足",
                rating=4,
                published_at=datetime(2026, 1, 1, tzinfo=UTC),
                source="fixture",
            ),
        ]
    )
    return CorpusRetriever(repo)


# 查询用「订阅」而非「订阅价格如何？」：corpus_fts 是 unicode61 严格短语匹配，
# 短语「订 阅 价 格 如 何」在 v1（订 阅 太 贵 了 …）中无法命中，检索为空；
# 「订阅」可稳定命中 v1 与 v3，才能走到生成 + 引用校验路径。
class _FakeProvider:
    def __init__(self, answer=None, error=False):
        self.answer = answer
        self.error = error

    def generate(self, system_prompt, user_prompt, schema):
        if self.error:
            raise RecoverableModelError("boom")
        if self.answer is not None:
            return schema.model_validate(self.answer)
        return schema(
            answer="用户普遍认为订阅价格偏高。",
            citations=[{"review_id": "v1", "quote": "订阅太贵了"}],
            evidence_sufficient=True,
            limitation="",
        )


def test_answer_with_valid_citations(retriever):
    answerer = RagAnswerer(_FakeProvider(), retriever)
    answer = answerer.answer("订阅", ["app-a"])
    assert isinstance(answer, RagAnswer)
    assert answer.evidence_sufficient
    assert answer.citations[0].review_id == "v1"


def test_answer_drops_fake_citations(retriever):
    provider = _FakeProvider(
        answer={
            "answer": "结论",
            "citations": [
                {"review_id": "v1", "quote": "订阅太贵了"},
                {"review_id": "FAKE-999", "quote": "不存在"},
            ],
            "evidence_sufficient": True,
            "limitation": "",
        }
    )
    answerer = RagAnswerer(provider, retriever)
    answer = answerer.answer("订阅", ["app-a"])
    assert [c.review_id for c in answer.citations] == ["v1"]


def test_answer_no_chunks_is_honest(retriever):
    answerer = RagAnswerer(_FakeProvider(), retriever)
    answer = answerer.answer("完全不相关的问题xyz", ["app-a"])
    assert not answer.evidence_sufficient
    assert answer.citations == []


def test_answer_model_failure_is_graceful(retriever):
    answerer = RagAnswerer(_FakeProvider(error=True), retriever)
    answer = answerer.answer("订阅", ["app-a"])
    assert not answer.evidence_sufficient
    assert "不可用" in answer.answer


def test_answer_cross_app_comparison(retriever):
    indexer = CorpusIndexer(retriever.agent_repository)
    indexer.index_reviews(
        [
            Review(
                review_id="v3",
                app_id="app-b",
                content_original="订阅流程顺畅",
                rating=5,
                published_at=datetime(2026, 1, 1, tzinfo=UTC),
                source="fixture",
            )
        ]
    )
    provider = _FakeProvider(
        answer={
            "answer": "两个 App 的订阅反馈不同。",
            "citations": [
                {"review_id": "v1", "quote": "订阅太贵了"},
                {"review_id": "v3", "quote": "订阅流程顺畅"},
            ],
            "evidence_sufficient": True,
            "limitation": "",
        }
    )
    answerer = RagAnswerer(provider, retriever)
    answer = answerer.answer("订阅", ["app-a", "app-b"])
    assert answer.answer
    assert answer.evidence_sufficient
    assert {c.review_id for c in answer.citations} == {"v1", "v3"}


def test_answer_all_citations_dropped_marks_insufficient(retriever):
    provider = _FakeProvider(
        answer={
            "answer": "结论",
            "citations": [{"review_id": "FAKE-1", "quote": "假的"}],
            "evidence_sufficient": True,
            "limitation": "",
        }
    )
    answerer = RagAnswerer(provider, retriever)
    answer = answerer.answer("订阅", ["app-a"])
    assert not answer.evidence_sufficient
    assert "未通过校验" in answer.limitation


def test_answer_model_honest_no_evidence_preserves_limitation(retriever):
    provider = _FakeProvider(
        answer={
            "answer": "结论",
            "citations": [],
            "evidence_sufficient": False,
            "limitation": "语料中没有提到价格问题",
        }
    )
    answerer = RagAnswerer(provider, retriever)
    answer = answerer.answer("订阅", ["app-a"])
    assert not answer.evidence_sufficient
    assert "语料中没有提到价格问题" in answer.limitation


def test_answer_provider_none_fallback(retriever):
    answerer = RagAnswerer(None, retriever)
    answer = answerer.answer("订阅", ["app-a"])
    assert answer.evidence_sufficient
    assert "模型未配置" in answer.limitation
    assert answer.citations  # 有检索结果的引用


def test_answer_rejects_empty_quote(retriever):
    provider = _FakeProvider(
        answer={
            "answer": "结论",
            "citations": [{"review_id": "v1", "quote": ""}],
            "evidence_sufficient": True,
            "limitation": "",
        }
    )
    answerer = RagAnswerer(provider, retriever)
    answer = answerer.answer("订阅", ["app-a"])
    assert not answer.evidence_sufficient
