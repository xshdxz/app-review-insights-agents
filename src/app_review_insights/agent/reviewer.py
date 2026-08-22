"""Reviewer Agent：确定性证据复核优先，LLM 抽查目标覆盖；模型失败时降级为纯确定性。"""

from __future__ import annotations

from typing import Any

from app_review_insights.agent.schemas import ReviewVerdict
from app_review_insights.models import RunStatus, Stage, ValidationReport
from app_review_insights.storage.repository import RunRepository

_REVIEWER_SYSTEM_PROMPT = (
    "你是产品情报系统的复核 Agent。检查分析结果是否覆盖用户目标。"
    "输出 JSON：{approved: bool, feedback: string}。\n"
    "规则：\n"
    "1. approve=true 当且仅当结果与目标相关且证据充分。\n"
    "2. 评论证据只能反映用户主观反馈（价格、功能、体验、客服等），"
    "无法提供转化率、收入、留存等运营指标。目标中属于运营指标的部分，"
    "只要评论证据边界内已覆盖用户相关痛点，即视为覆盖，不得以此拒绝。\n"
    "3. 拒绝时必须给出基于现有证据可实现的具体改进反馈（如某条发现证据薄弱、"
    "某个目标角度完全没有对应发现），不得索要评论数据无法提供的指标。"
)


def _render_review_prompt(goal: str, findings_summary: str = "") -> str:
    lines = [f"分析目标：{goal}"]
    if findings_summary:
        lines.append(f"\n分析发现摘要（证据边界）：\n{findings_summary}")
    lines.append("\n请复核结果是否覆盖该目标。")
    return "\n".join(lines)


def _findings_summary(findings: list[dict[str, Any]]) -> str:
    lines = []
    for index, finding in enumerate(findings, start=1):
        lines.append(
            f"{index}. {finding.get('title', '')} "
            f"（状态 {finding.get('evidence_status', '')} · "
            f"支持 {finding.get('support_count', 0)} / "
            f"冲突 {finding.get('conflict_count', 0)} · "
            f"置信度 {finding.get('confidence', 0)}）"
        )
    return "\n".join(lines)


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
        findings = self._load_findings(analysis_run_id)
        try:
            return self.provider.generate(
                _REVIEWER_SYSTEM_PROMPT,
                _render_review_prompt(goal, findings),
                ReviewVerdict,
            )
        except Exception:  # noqa: BLE001
            # 确定性检查已全部通过，LLM 抽查属增强性复核；
            # 任何模型侧失败都降级为通过（approved），保证复核流程可继续。
            return ReviewVerdict(approved=True, feedback="")

    def _load_findings(self, analysis_run_id: str) -> str:
        """读取已校验的发现摘要，让 LLM 复核时能看到证据边界。"""
        try:
            output = self.repository.get_output(analysis_run_id, Stage.VALIDATE_FINDINGS)
        except Exception:  # noqa: BLE001
            return ""
        if not output:
            return ""
        findings = output.get("findings", [])
        if not isinstance(findings, list):
            return ""
        return _findings_summary(findings)
