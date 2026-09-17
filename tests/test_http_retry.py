"""采集取数的瞬时故障重试。

第三方源抖动是真实数据采集的头号失败模式——本项目已经因此踩过 Apple RSS
接口变更（D-06）。一次 503 就让整轮分析失败，对使用者毫无道理。

关键在于**只重试瞬时的**：4xx 是确定性错误（URL 错了、被封了），
重试只是浪费对方配额，要让调用方立刻看到。
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from app_review_insights.collectors.http import get_with_retry, post_with_retry


class _FakeClient:
    """按脚本依次返回结果：异常实例会被抛出，其余作为响应返回。"""

    def __init__(self, script):
        self._script = list(script)
        self.calls = 0

    def get(self, url, **_kwargs):
        self.calls += 1
        item = self._script[min(self.calls - 1, len(self._script) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


def _response(status=200):
    return SimpleNamespace(status_code=status, url="https://example.invalid")


def _retry(url="https://example.invalid", **kwargs):
    slept: list[float] = []
    response = get_with_retry(kwargs.pop("client"), url, sleep=slept.append, **kwargs)
    return response, slept


# ── 成功路径 ─────────────────────────────────────────────────────────────────


def test_returns_immediately_on_success():
    client = _FakeClient([_response(200)])

    response, slept = _retry(client=client)

    assert response.status_code == 200
    assert client.calls == 1
    assert slept == []


def test_does_not_retry_deterministic_client_errors():
    """404 重试三次只是浪费对方配额，还拖慢失败反馈。"""
    client = _FakeClient([_response(404)])

    response, slept = _retry(client=client)

    assert response.status_code == 404
    assert client.calls == 1
    assert slept == []


# ── 瞬时故障 ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_retries_retryable_statuses_then_succeeds(status):
    client = _FakeClient([_response(status), _response(200)])

    response, slept = _retry(client=client)

    assert response.status_code == 200
    assert client.calls == 2
    assert len(slept) == 1


def test_retries_transport_errors_then_succeeds():
    client = _FakeClient([httpx.ConnectError("网络不可达"), _response(200)])

    response, slept = _retry(client=client)

    assert response.status_code == 200
    assert client.calls == 2


def test_backoff_grows_between_attempts():
    client = _FakeClient([_response(503), _response(503), _response(503), _response(200)])

    _, slept = _retry(client=client, attempts=4, base_delay=0.5)

    assert slept == [0.5, 1.0, 2.0]


def test_raises_after_exhausting_attempts():
    client = _FakeClient([_response(503)])

    with pytest.raises(httpx.HTTPError):
        _retry(client=client, attempts=3)

    assert client.calls == 3


def test_transport_error_is_reraised_after_exhausting_attempts():
    client = _FakeClient([httpx.ConnectError("一直连不上")])

    with pytest.raises(httpx.ConnectError):
        _retry(client=client, attempts=2)

    assert client.calls == 2


def test_single_attempt_never_sleeps():
    client = _FakeClient([_response(503)])

    with pytest.raises(httpx.HTTPError):
        _retry(client=client, attempts=1)

    assert client.calls == 1


def test_zero_base_delay_skips_sleep_entirely():
    """测试里把退避设为 0 就不该真的等待，否则测试会无谓变慢。"""
    client = _FakeClient([_response(503), _response(200)])

    _, slept = _retry(client=client, base_delay=0)

    assert slept == []


class _FakePostClient(_FakeClient):
    def post(self, url, **_kwargs):
        return self.get(url)


def test_post_with_retry_shares_the_same_policy():
    client = _FakePostClient([_response(503), _response(200)])
    slept: list[float] = []

    response = post_with_retry(client, "https://example.invalid", sleep=slept.append)

    assert response.status_code == 200
    assert client.calls == 2


def test_post_does_not_retry_deterministic_errors():
    client = _FakePostClient([_response(400)])

    response = post_with_retry(client, "https://example.invalid")

    assert response.status_code == 400
    assert client.calls == 1


def test_timeout_is_treated_as_transient():
    client = _FakeClient([httpx.ReadTimeout("读超时"), _response(200)])

    response, _ = _retry(client=client)

    assert response.status_code == 200
