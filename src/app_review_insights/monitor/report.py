"""报告生成器：把 Agent 运行的分析结果渲染为 Markdown 报告 + 与上次对比的变化摘要。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app_review_insights.models import (
    AgentRun,
    Finding,
    MonitorReport,
    Requirement,
    Stage,
    TestCase,
)
from app_review_insights.storage.agent_repository import AgentRepository
from app_review_insights.storage.repository import RunRepository


def build_report(
    agent_run: AgentRun,
    run_repository: RunRepository,
    agent_repository: AgentRepository,
    previous: MonitorReport | None = None,
) -> MonitorReport:
    run = run_repository.get_run(agent_run.analysis_run_id)
    findings = _load_findings(run_repository, run.run_id)
    requirements = _load_requirements(run_repository, run.run_id)
    test_cases = _load_test_cases(run_repository, run.run_id)

    markdown = render_report_markdown(agent_run, run, findings, requirements, test_cases)
    summary = (
        f"「{agent_run.goal}」分析完成：发现 {len(findings)} 条、"
        f"需求 {len(requirements)} 条、测试用例 {len(test_cases)} 条。"
        f"数据状态：{'有效' if run.status.value == 'completed' else run.status.value}。"
    )
    if previous is None:
        previous = agent_repository.latest_report(agent_run.app_url)
    changes = summarize_changes(previous, _bare_report(agent_run, summary, len(findings)))

    report = MonitorReport(
        report_id=str(uuid4()),
        agent_run_id=agent_run.run_id,
        app_url=agent_run.app_url,
        goal=agent_run.goal,
        markdown=markdown,
        summary=summary,
        findings_count=len(findings),
        changes=changes,
        created_at=datetime.now(UTC),
    )
    agent_repository.save_report(report)
    return report


def _bare_report(agent_run: AgentRun, summary: str, findings_count: int) -> MonitorReport:
    return MonitorReport(
        report_id="",
        agent_run_id=agent_run.run_id,
        app_url=agent_run.app_url,
        goal=agent_run.goal,
        markdown="",
        summary=summary,
        findings_count=findings_count,
        created_at=datetime.now(UTC),
    )


def _load_findings(run_repository: RunRepository, run_id: str) -> list[Finding]:
    output = run_repository.get_output(run_id, Stage.VALIDATE_FINDINGS)
    if not output:
        return []
    return [Finding.model_validate(item) for item in output.get("findings", [])]


def _load_requirements(run_repository: RunRepository, run_id: str) -> list[Requirement]:
    output = run_repository.get_output(run_id, Stage.PLAN)
    if not output:
        return []
    return [Requirement.model_validate(item) for item in output.get("requirements", [])]


def _load_test_cases(run_repository: RunRepository, run_id: str) -> list[TestCase]:
    output = run_repository.get_output(run_id, Stage.GENERATE_TESTS)
    if not output:
        return []
    return [TestCase.model_validate(item) for item in output.get("test_cases", [])]


def render_report_markdown(
    agent_run: AgentRun,
    run,
    findings: list[Finding],
    requirements: list[Requirement],
    test_cases: list[TestCase],
) -> str:
    lines = [
        "# 产品情报报告",
        "",
        f"- 目标：{agent_run.goal}",
        f"- App：{agent_run.app_url}",
        f"- 运行：`{run.run_id}`（状态 `{run.status.value}`）",
        "",
        f"## 发现（{len(findings)}）",
        "",
    ]
    for finding in findings:
        lines.extend(
            [
                f"### {finding.title}",
                "",
                f"- 状态：{finding.evidence_status.value} · 置信度 {finding.confidence} · "
                f"支持 {finding.support_count} / 冲突 {finding.conflict_count}",
                f"- 问题描述：{finding.problem_statement}",
                f"- 证据评论：{', '.join(finding.supporting_review_ids)}",
                "",
            ]
        )
    lines.extend([f"## 需求（{len(requirements)}）", ""])
    for requirement in requirements:
        lines.extend(
            [
                f"### {requirement.title}",
                "",
                f"- 优先级 {requirement.priority_score} · 影响 {requirement.impact}/5 · "
                f"复杂度 {requirement.complexity} · 目标版本 {requirement.target_version}",
                f"- 用户问题：{requirement.user_problem}",
                f"- 关联发现：{', '.join(requirement.finding_ids)}",
                "",
            ]
        )
    lines.extend([f"## 测试用例（{len(test_cases)}）", ""])
    for case in test_cases:
        lines.extend([f"- {case.test_case_id} {case.title}（{case.case_type}）", ""])
    return "\n".join(lines)


def summarize_changes(previous: MonitorReport | None, current: MonitorReport) -> list[str]:
    changes: list[str] = []
    if previous is None:
        return ["首次报告"]
    if current.findings_count != previous.findings_count:
        changes.append(f"发现数量变化：{previous.findings_count} → {current.findings_count}")
    return changes
