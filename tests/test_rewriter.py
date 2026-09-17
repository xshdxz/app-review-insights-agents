"""Tests for rag/rewriter.py — QueryRewriter query expansion."""

from app_review_insights.errors import RecoverableModelError
from app_review_insights.rag.rewriter import QueryRewriter


class _FakeProvider:
    def __init__(self, result=None, error=False):
        self.result = result
        self.error = error
        self.last_user_prompt = ""

    def generate(self, system_prompt, user_prompt, schema):
        self.last_user_prompt = user_prompt
        if self.error:
            raise RecoverableModelError("boom")
        if self.result is not None:
            return schema.model_validate(self.result)
        return schema(queries=["订阅价格", "续费扣费", "自动续订"])


def test_rewrite_returns_original_when_no_provider():
    rewriter = QueryRewriter(provider=None)
    result = rewriter.rewrite("订阅转化怎么样？")
    assert result == ["订阅转化怎么样？"]


def test_rewrite_expands_to_multiple_queries():
    provider = _FakeProvider()
    rewriter = QueryRewriter(provider=provider)
    result = rewriter.rewrite("订阅转化怎么样？")
    assert len(result) == 3
    assert result[0] == "订阅价格"
    assert "订阅转化怎么样？" in provider.last_user_prompt


def test_rewrite_deduplicates_case_insensitive():
    provider = _FakeProvider(result={"queries": ["billing", "Billing", "价格", "价格"]})
    rewriter = QueryRewriter(provider=provider)
    result = rewriter.rewrite("payment issues")
    assert len(result) == 2
    assert result == ["billing", "价格"]


def test_rewrite_returns_original_on_model_failure():
    provider = _FakeProvider(error=True)
    rewriter = QueryRewriter(provider=provider)
    result = rewriter.rewrite("订阅转化怎么样？")
    assert result == ["订阅转化怎么样？"]


def test_rewrite_returns_original_on_empty_queries():
    provider = _FakeProvider(result={"queries": ["", "  "]})
    rewriter = QueryRewriter(provider=provider)
    result = rewriter.rewrite("some question")
    assert result == ["some question"]


def test_rewrite_preserves_order():
    provider = _FakeProvider(result={"queries": ["a", "b", "c"]})
    rewriter = QueryRewriter(provider=provider)
    result = rewriter.rewrite("q")
    assert result == ["a", "b", "c"]
