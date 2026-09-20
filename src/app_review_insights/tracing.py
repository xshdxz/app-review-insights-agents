"""可选的三层追踪：run → stage → model call。

依赖是**可选**的：没装 opentelemetry 时这里全是 no-op，不多跑一行代码。
`pip install -e ".[observability]"` 之后把 `OTEL_EXPORTER_OTLP_ENDPOINT` 指到 collector，
就能在 Jaeger / Grafana Tempo 里看到三层嵌套 span。

为什么"可选 + 降级"而不是写进核心依赖：公开演示部署跑在免费层（1GB 限额），追踪是运维
增强而不是功能本身。核心依赖保持精简，需要的人一条命令装上即可。

**三层必须真的嵌套**（run 包 stage、stage 包 model），否则"三层追踪"只是三个平级 span——
那在时间轴上读不出"这段时间花在哪个阶段的哪次调用上"。因此 stage 这类跨函数边界的 span
用手动 start/end，并把它挂成 current context（见 `start_span`）。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger("ari-tracing")

try:  # pragma: no cover - 取决于是否装了可选依赖
    from opentelemetry import trace
    from opentelemetry.context import attach, detach
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider

    _OTEL_AVAILABLE = True
except Exception:  # noqa: BLE001 - 缺依赖是正常状态，不是错误
    _OTEL_AVAILABLE = False

_tracer: Any = None


def otel_available() -> bool:
    """可选依赖是否在场。"""
    return _OTEL_AVAILABLE


def reset_for_tests() -> None:
    """把模块状态复位（仅供测试注入假 tracer 后清理）。"""
    global _tracer
    _tracer = None


#: 只有配了这个环境变量才真的上报。默认**什么都不做**——应用不该在没人配置的情况下
#: 往一个不存在的 collector 发数据：那会变成一堆超时与错误日志，还会拖慢测试。
OTLP_ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"


def configure_tracing(service_name: str = "app-review-insights") -> bool:
    """装配追踪；返回是否真的启用。

    启用条件：装了可选依赖 **且** 配了 `OTEL_EXPORTER_OTLP_ENDPOINT`。
    缺依赖、缺端点、装配抛错——一律返回 False 并留痕，绝不向上抛：
    观测能力不能反过来拦住应用启动。
    """
    global _tracer
    if not _OTEL_AVAILABLE:
        return False
    if _tracer is not None:
        return True
    endpoint = os.environ.get(OTLP_ENDPOINT_ENV, "").strip()
    if not endpoint:
        return False
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        exporter: Any = OTLPSpanExporter(endpoint=endpoint)
        provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer(service_name)
        return True
    except Exception:
        logger.warning("追踪装配失败（不影响主流程）", exc_info=True)
        return False


class SpanHandle:
    """手动管理的 span：附带把它设为 current 的 context token。"""

    __slots__ = ("span", "token")

    def __init__(self, span: Any) -> None:
        self.span = span
        # 把 stage 的 span 挂成 current，stage 内部的 model span 才会成为它的子节点
        self.token = attach(trace.set_span_in_context(span)) if _OTEL_AVAILABLE else None


def start_span(name: str, **attributes: Any) -> SpanHandle | None:
    """跨函数边界时手动开一个 span；追踪未启用时返回 None（调用方无需分支）。"""
    if _tracer is None:
        return None
    try:
        span_obj = _tracer.start_span(name)
        for key, value in attributes.items():
            span_obj.set_attribute(key, value)
        return SpanHandle(span_obj)
    except Exception:
        logger.warning("span 创建失败（不影响主流程）name=%s", name, exc_info=True)
        return None


def end_span(handle: SpanHandle | None) -> None:
    if handle is None:
        return
    try:
        if handle.token is not None:
            detach(handle.token)
        handle.span.end()
    except Exception:
        logger.warning("span 结束失败（不影响主流程）", exc_info=True)


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """作用域内的 span（用于一次调用这种边界清楚的场景）。未启用时零成本。"""
    if _tracer is None:
        yield None
        return
    try:
        with _tracer.start_as_current_span(name) as current:
            for key, value in attributes.items():
                current.set_attribute(key, value)
            yield current
    except Exception:
        logger.warning("span 执行失败（不影响主流程）name=%s", name, exc_info=True)
        yield None
