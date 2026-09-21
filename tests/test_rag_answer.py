from datetime import UTC, datetime

import pytest

from app_review_insights.errors import RecoverableModelError
from app_review_insights.models import Review
from app_review_insights.rag.answer import RagAnswerer, gather_evidence
from app_review_insights.rag.indexer import CorpusIndexer
from app_review_insights.rag.retrieval import CorpusRetriever, RetrievedChunk
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


# --- D-18：改写只在「原查询零结果」时触发 --------------------------------------
# 策略收敛在 rag.answer.gather_evidence 一处，UI 与评测脚本走同一个函数——
# 否则量出来的就不是上线那条路。


class _StubRetriever:
    """按查询字面量返回预置结果，并记录收到的查询。"""

    def __init__(self, mapping: dict[str, list[RetrievedChunk]]):
        self.mapping = mapping
        self.queries: list[str] = []

    def search_many(self, query, app_ids, top_k=10):
        self.queries.append(query)
        return list(self.mapping.get(query, []))[:top_k]


class _StubRewriter:
    def __init__(self, queries: list[str]):
        self.queries = queries
        self.calls = 0

    def rewrite(self, question: str) -> list[str]:
        self.calls += 1
        return list(self.queries)


def _chunk(review_id: str, score: float) -> RetrievedChunk:
    return RetrievedChunk(
        review_id=review_id,
        app_id="app-a",
        content=f"内容 {review_id}",
        score=score,
        platform="app-store",
        source="fixture",
    )


def test_original_query_hit_skips_rewrite():
    """原查询有命中时不得改写：跨查询比分数正是 D-18 的负收益来源。"""
    retriever = _StubRetriever({"订阅价格": [_chunk("v1", 1.0)]})
    rewriter = _StubRewriter(["续费"])
    chunks = gather_evidence(retriever, "订阅价格", ["app-a"], top_k=5, rewriter=rewriter)
    assert [chunk.review_id for chunk in chunks] == ["v1"]
    assert rewriter.calls == 0
    assert retriever.queries == ["订阅价格"]


def test_zero_result_triggers_rewrite():
    """零结果时才启用改写：此时改写只可能「从无到有」，挤不掉任何原查询结果。"""
    retriever = _StubRetriever({"没人问过的问题": [], "续费": [_chunk("v9", 1.0)]})
    rewriter = _StubRewriter(["续费"])
    chunks = gather_evidence(retriever, "没人问过的问题", ["app-a"], top_k=5, rewriter=rewriter)
    assert [chunk.review_id for chunk in chunks] == ["v9"]
    assert rewriter.calls == 1


def test_rewrite_fallback_merge_is_rank_based():
    """回退路的合并按名次轮转，不跨查询比分数——后者会让某条查询的结果被整体挤下去。"""
    retriever = _StubRetriever(
        {
            "没人问过的问题": [],
            "a": [_chunk("v1", 1.0), _chunk("v2", 0.98)],
            "b": [_chunk("v3", 0.95)],
        }
    )
    rewriter = _StubRewriter(["a", "b"])
    chunks = gather_evidence(retriever, "没人问过的问题", ["app-a"], top_k=5, rewriter=rewriter)
    assert [chunk.review_id for chunk in chunks] == ["v1", "v3", "v2"]


def test_rewrite_deduplicates_original_question():
    """改写把原问题原样返回时不再重复检索（省一次查询，语义不变）。"""
    retriever = _StubRetriever({"没人问过的问题": []})
    rewriter = _StubRewriter(["没人问过的问题"])
    chunks = gather_evidence(retriever, "没人问过的问题", ["app-a"], top_k=5, rewriter=rewriter)
    assert chunks == []
    assert retriever.queries == ["没人问过的问题"]


def test_answerer_does_not_rewrite_when_original_query_hits(retriever):
    spy = _StubRewriter(["续费", "自动扣费"])
    answerer = RagAnswerer(_FakeProvider(), retriever, rewriter=spy)
    answer = answerer.answer("订阅", ["app-a"])
    assert answer.evidence_sufficient
    assert spy.calls == 0


def test_answerer_rewrite_rescues_zero_result_question(retriever):
    """端到端：原查询零结果时，改写能把证据救回来（走真实 FTS）。"""
    answerer = RagAnswerer(None, retriever, rewriter=_StubRewriter(["续费"]))
    answer = answerer.answer("完全没有出现过的问句zzz", ["app-a"])
    assert answer.evidence_sufficient
    assert answer.citations[0].review_id == "v1"
