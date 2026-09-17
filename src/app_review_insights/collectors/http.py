"""采集取数的瞬时故障重试。

第三方源抖动是真实数据采集的头号失败模式——本项目已经因此踩过 Apple RSS
接口变更（D-06）。一次 503 就让整轮分析失败，对使用者毫无道理。

关键在于**只重试瞬时的**：4xx（429 除外）是确定性错误——URL 错了、被封了——
重试只是浪费对方配额并拖慢失败反馈，应当立刻交给调用方处理。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

#: 值得重试的状态码：限流与各类服务端临时故障
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

DEFAULT_ATTEMPTS = 3
DEFAULT_BASE_DELAY = 0.5


def _with_retry(
    send: Callable[[], httpx.Response],
    *,
    attempts: int,
    base_delay: float,
    sleep: Callable[[float], None],
) -> httpx.Response:
    attempts = max(1, attempts)
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            response = send()
        except httpx.HTTPError as exc:
            last_error = exc
        else:
            if response.status_code not in RETRYABLE_STATUS:
                return response
            last_error = httpx.HTTPError(
                f"HTTP {response.status_code}（已尝试 {attempt + 1}/{attempts} 次）"
            )
        if attempt < attempts - 1 and base_delay > 0:
            sleep(base_delay * (2**attempt))
    if last_error is None:  # pragma: no cover - attempts>=1 时不可能走到
        raise httpx.HTTPError("请求失败且没有可用错误信息")
    raise last_error


def get_with_retry(
    client: Any,
    url: str,
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY,
    sleep: Callable[[float], None] = time.sleep,
    **kwargs: Any,
) -> httpx.Response:
    """带指数退避的 GET。

    只对瞬时故障重试；其它状态码原样返回，由调用方 `raise_for_status()` 决定。
    重试耗尽后抛出最后一个异常，语义与直接失败一致。
    `sleep` 可注入；测试里传 `base_delay=0` 即可完全跳过等待。
    """
    return _with_retry(
        lambda: client.get(url, **kwargs),
        attempts=attempts,
        base_delay=base_delay,
        sleep=sleep,
    )


def post_with_retry(
    client: Any,
    url: str,
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY,
    sleep: Callable[[float], None] = time.sleep,
    **kwargs: Any,
) -> httpx.Response:
    """带指数退避的 POST；语义同 :func:`get_with_retry`。"""
    return _with_retry(
        lambda: client.post(url, **kwargs),
        attempts=attempts,
        base_delay=base_delay,
        sleep=sleep,
    )
