"""响应缓存：同输入不再付费，但**绝不能**跨请求串味。

这里最重要的一条不是"命中"，而是"该未命中时必须未命中"：缓存键漏掉模型或温度，
就不是缓存而是"把别的请求的答案返回给你"——那是正确性事故。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import BaseModel

from app_review_insights.config import Settings
from app_review_insights.factory import _build_model_provider
from app_review_insights.llm.cache import CachingProvider, ResponseCache, cache_key
from app_review_insights.storage import RunRepository


class _Payload(BaseModel):
    value: str


_PAYLOAD = {"value": "模型答案"}


class _CountingProvider:
    """记录被真正调用了几次。缓存有没有生效，看这个计数最直接。"""

    def __init__(self, *, model: str = "deepseek-chat", temperature: float = 0.1, max_tokens=8192):
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.calls = 0

    def generate(self, system_prompt, user_prompt, schema):
        self.calls += 1
        return schema.model_validate(_PAYLOAD)


@pytest.fixture
def cache(tmp_path) -> ResponseCache:
    return ResponseCache(tmp_path / "cache.sqlite3", ttl_days=7)


def test_same_request_is_served_from_cache(cache):
    inner = _CountingProvider()
    provider = CachingProvider(inner, cache)

    first = provider.generate("system", "user", _Payload)
    second = provider.generate("system", "user", _Payload)

    assert inner.calls == 1, "第二次必须走缓存，不能再调用模型"
    assert first == second
    stats = cache.stats()
    assert (stats.hits, stats.misses) == (1, 1)
    assert stats.hit_rate == 0.5


def test_different_model_does_not_hit_the_cache(cache):
    """正确性护栏：换了模型还能命中，就等于把别的模型的答案当成这次的答案。"""
    provider_a = CachingProvider(_CountingProvider(model="deepseek-chat"), cache)
    provider_a.generate("system", "user", _Payload)

    inner_b = _CountingProvider(model="deepseek-reasoner")
    CachingProvider(inner_b, cache).generate("system", "user", _Payload)

    assert inner_b.calls == 1, "换了模型必须重新调用"


@pytest.mark.parametrize(
    "changed",
    [
        {"temperature": 0.7},
        {"max_tokens": 4096},
    ],
)
def test_changing_sampling_parameters_does_not_hit_the_cache(cache, changed):
    CachingProvider(_CountingProvider(), cache).generate("system", "user", _Payload)

    inner = _CountingProvider(**changed)
    CachingProvider(inner, cache).generate("system", "user", _Payload)

    assert inner.calls == 1, f"改了 {changed} 必须重新调用"


def test_different_prompt_or_schema_does_not_hit_the_cache(cache):
    first = _CountingProvider()
    CachingProvider(first, cache).generate("system", "user-A", _Payload)

    other_prompt = _CountingProvider()
    CachingProvider(other_prompt, cache).generate("system", "user-B", _Payload)
    assert other_prompt.calls == 1

    class _OtherPayload(BaseModel):
        value: str

    other_schema = _CountingProvider()
    CachingProvider(other_schema, cache).generate("system", "user-A", _OtherPayload)
    assert other_schema.calls == 1


def test_expired_entries_are_treated_as_misses_and_pruned(cache):
    key = cache_key(
        model="deepseek-chat",
        temperature=0.1,
        max_tokens=8192,
        schema_name="_Payload",
        system="system",
        user="user",
    )
    long_ago = datetime.now(UTC) - timedelta(days=30)
    cache.put(key, schema_name="_Payload", model="deepseek-chat", response=_PAYLOAD, now=long_ago)

    assert cache.get(key) is None, "超过 TTL 的条目必须视为未命中"
    assert cache.prune() == 1
    assert cache.stats().entries == 0


def test_corrupt_entry_is_refetched_instead_of_returned(cache):
    """缓存条目与 Schema 对不上时按未命中处理——绝不能把坏数据交给调用方。"""
    key = cache_key(
        model="deepseek-chat",
        temperature=0.1,
        max_tokens=8192,
        schema_name="_Payload",
        system="system",
        user="user",
    )
    cache.put(key, schema_name="_Payload", model="deepseek-chat", response={"unexpected": 1})

    inner = _CountingProvider()
    result = CachingProvider(inner, cache).generate("system", "user", _Payload)

    assert inner.calls == 1, "坏条目必须触发重新调用"
    assert result.value == "模型答案"


def test_factory_only_wraps_the_cache_when_enabled(tmp_path):
    """接线测试：模块写了却没人用，是最容易漏的一种"完成"。"""
    database = tmp_path / "runs.sqlite3"
    cache_path = tmp_path / "cache.sqlite3"

    enabled = Settings(
        DEEPSEEK_API_KEY="sk-cache-fixture",
        DATABASE_PATH=str(database),
        MODEL_CACHE_PATH=str(cache_path),
        MODEL_CACHE_ENABLED=True,
    )
    disabled = Settings(
        DEEPSEEK_API_KEY="sk-cache-fixture",
        DATABASE_PATH=str(database),
        MODEL_CACHE_PATH=str(cache_path),
        MODEL_CACHE_ENABLED=False,
    )

    wrapped = _build_model_provider(enabled, RunRepository(database), input_fingerprint=None)
    plain = _build_model_provider(disabled, RunRepository(database), input_fingerprint=None)

    assert isinstance(wrapped, CachingProvider)
    assert not isinstance(plain, CachingProvider)
    assert wrapped.model == enabled.model_name
