"""模型调用用量与成本计量。

LLM 应用的生命线：一次分析花了多少 token、多少钱，必须有账可查。

- provider 每次拿到响应后把用量交给注入的 recorder（校验失败也算，因为已经计费）；
- orchestrator 通过 contextvar 提供 `run_id` / `stage`，使账目能按运行与阶段归集。

价格是**估算口径**：用于发现成本异常与比较阶段开销，精确账单以供应商为准。
"""

from __future__ import annotations

from contextvars import ContextVar
from datetime import UTC, datetime

from pydantic import BaseModel, Field

#: 每 100 万 token 的价格（美元），格式 (输入, 输出)；按模型名前缀匹配。
MODEL_PRICING: dict[str, tuple[float, float]] = {
    "deepseek-chat": (0.27, 1.10),
    "deepseek-reasoner": (0.55, 2.19),
    "gpt-4o-mini": (0.15, 0.60),
}

#: 未登记模型的价格兜底，避免因为缺报价就把账丢掉。
DEFAULT_PRICING: tuple[float, float] = (0.27, 1.10)

_TOKENS_PER_UNIT = 1_000_000

current_run_id: ContextVar[str | None] = ContextVar("ari_run_id", default=None)
current_stage: ContextVar[str | None] = ContextVar("ari_stage", default=None)


def resolve_pricing(model: str) -> tuple[float, float]:
    """按前缀匹配价格档；未登记的模型回退默认档。"""
    for prefix, price in MODEL_PRICING.items():
        if model.startswith(prefix):
            return price
    return DEFAULT_PRICING


def estimate_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """估算一次调用的费用（美元）。"""
    input_price, output_price = resolve_pricing(model)
    return (prompt_tokens * input_price + completion_tokens * output_price) / _TOKENS_PER_UNIT


class ModelUsage(BaseModel):
    """一次模型调用的用量账目。"""

    model: str
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    latency_ms: float = Field(ge=0)
    estimated_cost_usd: float = Field(ge=0)
    run_id: str | None = None
    stage: str | None = None
    created_at: datetime


def build_usage(
    *,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: float,
    run_id: str | None = None,
    stage: str | None = None,
) -> ModelUsage:
    """构造账目；未显式传入的 `run_id`/`stage` 从 contextvar 取。"""
    return ModelUsage(
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        latency_ms=latency_ms,
        estimated_cost_usd=estimate_cost_usd(model, prompt_tokens, completion_tokens),
        run_id=run_id if run_id is not None else current_run_id.get(),
        stage=stage if stage is not None else current_stage.get(),
        created_at=datetime.now(UTC),
    )
