"""模型调用预算熔断。

没有预算的 LLM 应用等于不可控开支：一次误操作的分析可能烧掉整月额度。

超预算时抛 ModelBudgetExceeded（RecoverableModelError 的子类），
让流水线停在检查点上——已完成的工作全部保留，调高预算即可续跑。

守卫通过注入的 spent_usd(run_id, since) 查询已花费金额，因此本模块不依赖
存储层，避免与 storage.repository 形成导入环。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, time

from app_review_insights.errors import ModelBudgetExceeded
from app_review_insights.llm.usage import current_run_id

#: 查询已花费金额：(run_id, since) -> 美元。两个参数均可为 None 表示不限。
SpentUsd = Callable[[str | None, datetime | None], float]


def start_of_today_utc(now: datetime | None = None) -> datetime:
    """今天 00:00（UTC），用于「每日预算」的时间窗。"""
    moment = now or datetime.now(UTC)
    return datetime.combine(moment.date(), time.min, tzinfo=UTC)


def make_budget_guard(
    spent_usd: SpentUsd,
    *,
    per_run_usd: float = 0.0,
    per_day_usd: float = 0.0,
) -> Callable[[], None] | None:
    """构造预算守卫；未配置任何预算时返回 None（不产生额外查询开销）。"""
    if per_run_usd <= 0 and per_day_usd <= 0:
        return None

    def _check() -> None:
        if per_run_usd > 0:
            spent = spent_usd(current_run_id.get(), None)
            if spent >= per_run_usd:
                raise ModelBudgetExceeded(
                    f"本次运行模型费用已达上限：${spent:.4f} / ${per_run_usd:.4f}。"
                    "已完成的工作已保存在检查点；调高 MODEL_BUDGET_USD_PER_RUN 后"
                    "可用同一 run_id 续跑。"
                )
        if per_day_usd > 0:
            spent_today = spent_usd(None, start_of_today_utc())
            if spent_today >= per_day_usd:
                raise ModelBudgetExceeded(
                    f"今日模型费用已达上限：${spent_today:.4f} / ${per_day_usd:.4f}。"
                    "调高 MODEL_BUDGET_USD_PER_DAY，或次日再试。"
                )

    return _check
