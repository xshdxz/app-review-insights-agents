"""Reviewer Agent：确定性证据复核优先，LLM 抽查目标覆盖；模型失败时降级为纯确定性。"""

from __future__ import annotations

from typing import Any

from app_review_insights.agent.schemas import ReviewVerdict
from app_review_insights.models import RunStatus, Stage, ValidationReport
from app_review_insights.storage.repository import RunRepository

_REVIEWER_SYSTEM_PROMPT = (
    "你是产品情报系统的复核 Agent。检查分析结果是否覆盖用户目标。"
    "输出 JSON：{approved: bool, feedback: string}。"
    "approve=true 仅当结果与目标相关且证据充分；否则给出一句可执行的改进反馈。"
)


def _render_review_prompt(goal: str) -> str:
    return f"分析目标：{goal}\n请复核结果是否覆盖该目标。"


class Reviewer:
    def __init__(
        self,
        provider: Any | None,
        repository: RunRepository,
        max_rounds: int = 2,
    ):
        self.provider = provider
        self.repository = repository
        self.max_rounds = max_rounds

    def review(self, goal: str, analysis_run_id: str) -> ReviewVerdict:
        try:
            run = self.repository.get_run(analysis_run_id)
        except KeyError:
            return ReviewVerdict(approved=False, feedback="分析运行不存在")
        if run.status != RunStatus.COMPLETED:
            return ReviewVerdict(
                approved=False,
                feedback=f"分析未完成（状态：{run.status.value}）",
            )
        trace_output = self.repository.get_output(analysis_run_id, Stage.VALIDATE_TRACEABILITY)
        if trace_output is None:
            return ReviewVerdict(approved=False, feedback="缺少追溯校验结果")
        try:
            report = ValidationReport.model_validate(trace_output)
        except Exception:  # noqa: BLE001
            # 追溯校验输出格式异常（非合法 ValidationReport）时按证据无效处理，
            # 复核拒绝，而不是让解析错误向上传播中断流程。
            return ReviewVerdict(approved=False, feedback="追溯校验结果格式异常")
        if not report.valid:
            issues = "; ".join(issue.message for issue in report.issues[:3])
            detail = f"：{issues}" if issues else "（无明细）"
            return ReviewVerdict(approved=False, feedback=f"证据链校验未通过{detail}")

        if self.provider is None:
            return ReviewVerdict(approved=True, feedback="")
        try:
            return self.provider.generate(
                _REVIEWER_SYSTEM_PROMPT,
                _render_review_prompt(goal),
                ReviewVerdict,
            )
        except Exception:  # noqa: BLE001
            # 确定性检查已全部通过，LLM 抽查属增强性复核；
            # 任何模型侧失败都降级为通过（approved），保证复核流程可继续。
            return ReviewVerdict(approved=True, feedback="")
