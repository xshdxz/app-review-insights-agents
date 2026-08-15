from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import pandas as pd
import streamlit as st

from app_review_insights.models import RunRecord, SourceType, Stage, StageEvent
from app_review_insights.storage import RunRepository

STATUS_LABELS = {
    "pending": "待开始",
    "running": "运行中",
    "waiting_for_model": "等待模型恢复",
    "partial": "部分完成",
    "completed": "已完成",
    "failed": "失败",
}

STAGE_LABELS = {
    "scope": "定义范围",
    "collect": "采集或导入",
    "clean": "确定性清洗",
    "analyze_batches": "分批语义分析",
    "consolidate": "归并发现",
    "validate_findings": "证据校验",
    "plan": "产品规划",
    "generate_tests": "生成测试",
    "validate_traceability": "追溯校验",
    "complete": "完成",
}

_REVIEW_COLUMNS = (
    "review_id",
    "rating",
    "app_version",
    "published_at",
    "language",
    "content_original",
    "content_summary_zh",
    "source",
)
_TEST_CASE_COLUMNS = (
    "test_case_id",
    "requirement_id",
    "title",
    "case_type",
    "preconditions",
    "steps",
    "expected_result",
    "source_review_ids",
)
_ISSUE_COLUMNS = (
    "severity",
    "entity_type",
    "entity_id",
    "rule",
    "message",
    "revision_action",
)


def _records_frame(
    records: Iterable[dict[str, Any]],
    allowed_columns: Iterable[str],
) -> pd.DataFrame:
    columns = list(allowed_columns)
    safe_records = [
        {column: record.get(column) for column in columns if column in record} for record in records
    ]
    return pd.DataFrame(safe_records, columns=columns).dropna(axis=1, how="all")


def render_provenance_legend() -> None:
    with st.container(horizontal=True, gap="small"):
        st.badge(
            "Deterministic",
            icon=":material/functions:",
            color="blue",
            help="由程序规则直接计算，不依赖语言模型。",
        )
        st.badge(
            "AI-generated",
            icon=":material/auto_awesome:",
            color="orange",
            help="由 DeepSeek 生成，必须结合证据与校验状态审阅。",
        )
        st.badge(
            "Validated",
            icon=":material/verified:",
            color="green",
            help="已通过当前阶段的确定性校验。",
        )
        st.badge(
            "Assumption",
            icon=":material/help:",
            color="gray",
            help="证据不足或仍需人工确认，不能视为确定事实。",
        )


def render_model_status(model_ready: bool, model_name: str) -> None:
    if model_ready:
        st.success(
            f"模型状态：{model_name} 已配置，可开始实时语义分析。",
            icon=":material/cloud_done:",
        )
    else:
        st.warning(
            "模型状态：未配置 DEEPSEEK_API_KEY。档案库仍可读取，但实时开始与继续分析暂不可用。",
            icon=":material/key_off:",
        )


def render_run_status(run: RunRecord, events: list[StageEvent]) -> None:
    with st.container(border=True):
        st.subheader("运行状态", anchor=False)
        st.progress(run.coverage_ratio, text=f"分析覆盖率 {run.coverage_ratio:.0%}")

        with st.container(horizontal=True, gap="small"):
            status_color = {
                "completed": "green",
                "running": "blue",
                "waiting_for_model": "orange",
                "partial": "yellow",
                "failed": "red",
            }.get(run.status.value, "gray")
            st.badge(STATUS_LABELS.get(run.status.value, run.status.value), color=status_color)
            st.badge(
                STAGE_LABELS.get(run.current_stage.value, run.current_stage.value),
                color="primary",
            )

        st.caption(f"Run ID：`{run.run_id}`")
        st.write(f"当前 stage：`{run.current_stage.value}`")
        if run.total_batches:
            st.write(f"batch 检查点：`已完成 {run.current_batch} / {run.total_batches}`")
        else:
            st.write("当前 batch：`尚未分批`")

        if run.last_error:
            st.warning(run.last_error, icon=":material/report_problem:")

        with st.expander("最近事件", icon=":material/history:"):
            if not events:
                st.caption("尚无事件。")
            for event in reversed(events[-6:]):
                timestamp = event.created_at.astimezone().strftime("%m-%d %H:%M:%S")
                st.caption(
                    f"{timestamp} · {STAGE_LABELS.get(event.stage.value, event.stage.value)}"
                )
                st.write(event.message)


def render_records(
    records: list[dict[str, Any]],
    empty_message: str,
    allowed_columns: Iterable[str],
    *,
    key: str,
) -> None:
    if not records:
        st.info(empty_message, icon=":material/inbox:")
        return
    st.dataframe(
        _records_frame(records, allowed_columns),
        hide_index=True,
        key=key,
    )


def _run_limitations(run: RunRecord, clean: dict[str, Any]) -> list[str]:
    stats = clean.get("stats", {})
    if run.request.source_type != SourceType.ONLINE or "input_count" not in stats:
        return []
    actual_count = int(stats["input_count"])
    if actual_count >= run.request.review_limit:
        return []
    return [
        f"在线采集目标 {run.request.review_limit} 条，实际获得 {actual_count} 条；"
        "样本短缺会降低证据覆盖度。"
    ]


def _render_overview(
    clean: dict[str, Any],
    findings: dict[str, Any],
    plan: dict[str, Any],
    test_output: dict[str, Any],
    run_limitations: list[str],
) -> None:
    st.badge("Deterministic", icon=":material/functions:", color="blue")
    stats = clean.get("stats", {})
    with st.container(horizontal=True):
        st.metric("输入评论", stats.get("input_count", 0), border=True)
        st.metric("保留评论", stats.get("output_count", 0), border=True)
        st.metric(
            "去重评论",
            stats.get("exact_duplicates", 0) + stats.get("near_duplicates", 0),
            border=True,
        )
        st.metric("移除空内容", stats.get("empty_removed", 0), border=True)
    for limitation in run_limitations:
        st.warning(limitation, icon=":material/data_alert:")

    st.badge("AI-generated", icon=":material/auto_awesome:", color="orange")
    with st.container(horizontal=True):
        st.metric("Findings", len(findings.get("findings", [])), border=True)
        st.metric("PRD 需求", len(plan.get("requirements", [])), border=True)
        st.metric("测试用例", len(test_output.get("test_cases", [])), border=True)


def _render_findings(findings_output: dict[str, Any]) -> None:
    findings = findings_output.get("findings", [])
    if not findings:
        st.info("尚无 Findings。模型暂停时，已完成阶段的结果仍会保留。")
        return

    for finding in findings:
        status = finding.get("evidence_status", "assumption")
        status_label = "Validated" if status == "validated" else "Assumption"
        status_color = "green" if status == "validated" else "gray"
        with st.expander(
            f"{finding.get('finding_id', 'Finding')} · {finding.get('title', '未命名发现')}",
            icon=":material/article:",
        ):
            with st.container(horizontal=True, gap="small"):
                st.badge("AI-generated", color="orange")
                st.badge(status_label, color=status_color)
            st.write(finding.get("problem_statement", ""))
            with st.container(horizontal=True):
                st.metric("支持证据", finding.get("support_count", 0), border=True)
                st.metric("冲突证据", finding.get("conflict_count", 0), border=True)
                st.metric(
                    "置信度",
                    f"{float(finding.get('confidence', 0)):.0%}",
                    border=True,
                )
            st.markdown(f"**主题标签**：{finding.get('topic_label', '—')}")
            st.markdown("**支持评论 ID**：" + ", ".join(finding.get("supporting_review_ids", [])))
            conflicts = finding.get("conflicting_review_ids", [])
            st.markdown("**冲突评论 ID**：" + (", ".join(conflicts) or "无"))
            st.markdown(f"**模型归纳说明**：{finding.get('model_reasoning_summary', '—')}")
            limitations = finding.get("limitations", [])
            if limitations:
                st.warning("\n".join(f"- {item}" for item in limitations))


def _render_requirements(plan_output: dict[str, Any]) -> None:
    if plan_output.get("quantity_notice"):
        st.info(plan_output["quantity_notice"], icon=":material/info:")
    requirements = plan_output.get("requirements", [])
    if not requirements:
        st.info("尚无 PRD 需求。")
        return

    for requirement in requirements:
        with st.expander(
            f"{requirement.get('requirement_id', 'REQ')} · "
            f"{requirement.get('target_version', 'Future')} · "
            f"{requirement.get('title', '未命名需求')}",
            icon=":material/description:",
        ):
            with st.container(horizontal=True, gap="small"):
                st.badge("AI-generated", color="orange")
                if requirement.get("assumptions"):
                    st.badge("Assumption", color="gray")
            st.markdown(f"**用户问题**：{requirement.get('user_problem', '—')}")
            st.markdown(f"**目标**：{requirement.get('objective', '—')}")
            st.markdown(
                f"**优先级分**：{requirement.get('priority_score', 0)} · "
                f"**影响**：{requirement.get('impact', '—')} · "
                f"**复杂度**：{requirement.get('complexity', '—')}"
            )
            fields = (
                ("范围", "scope"),
                ("非目标", "non_goals"),
                ("功能规则", "functional_rules"),
                ("边界情况", "edge_cases"),
                ("验收标准", "acceptance_criteria"),
                ("成功指标", "success_metrics"),
                ("假设", "assumptions"),
            )
            for label, field in fields:
                values = requirement.get(field, [])
                if values:
                    st.markdown(f"**{label}**")
                    st.markdown("\n".join(f"- {value}" for value in values))
            st.caption(
                "Finding："
                + ", ".join(requirement.get("finding_ids", []))
                + " · Review："
                + ", ".join(requirement.get("source_review_ids", []))
            )


def _traceability_rows(
    requirements: list[dict[str, Any]],
    test_cases: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    cases_by_requirement: dict[str, list[str]] = {}
    for case in test_cases:
        cases_by_requirement.setdefault(case.get("requirement_id", ""), []).append(
            case.get("test_case_id", "")
        )
    return [
        {
            "requirement_id": requirement.get("requirement_id"),
            "finding_ids": requirement.get("finding_ids", []),
            "review_ids": requirement.get("source_review_ids", []),
            "test_case_ids": cases_by_requirement.get(requirement.get("requirement_id", ""), []),
        }
        for requirement in requirements
    ]


def render_result_tabs(
    repository: RunRepository,
    run_id: str,
    events: list[StageEvent],
) -> None:
    run = repository.get_run(run_id)
    outputs = {
        stage.value: repository.get_output(run_id, stage)
        for stage in (
            Stage.CLEAN,
            Stage.VALIDATE_FINDINGS,
            Stage.PLAN,
            Stage.GENERATE_TESTS,
            Stage.VALIDATE_TRACEABILITY,
        )
    }
    render_result_payloads(run, outputs, run_id, events)


def render_result_payloads(
    run: RunRecord,
    outputs: Mapping[str, dict[str, Any] | None],
    result_key: str,
    events: list[StageEvent],
) -> None:
    clean = outputs.get(Stage.CLEAN.value) or {"reviews": [], "stats": {}}
    findings = outputs.get(Stage.VALIDATE_FINDINGS.value) or {
        "findings": [],
        "report": {"valid": False, "issues": []},
    }
    plan = outputs.get(Stage.PLAN.value) or {"requirements": []}
    test_output = outputs.get(Stage.GENERATE_TESTS.value) or {"test_cases": []}
    trace_output = outputs.get(Stage.VALIDATE_TRACEABILITY.value)
    trace = trace_output or {"valid": False, "issues": []}
    run_limitations = _run_limitations(run, clean)

    tabs = st.tabs(
        [
            ":material/analytics: 总览",
            ":material/reviews: 评论证据",
            ":material/lightbulb: Findings",
            ":material/description: PRD",
            ":material/checklist: 测试用例",
            ":material/account_tree: 证据链",
            ":material/receipt_long: 日志与限制",
        ]
    )
    with tabs[0]:
        _render_overview(clean, findings, plan, test_output, run_limitations)
    with tabs[1]:
        st.caption("Deterministic · 原始评论文本保留为证据，中文摘要不能替代原文。")
        render_records(
            clean.get("reviews", []),
            "尚无清洗评论。",
            _REVIEW_COLUMNS,
            key=f"reviews-{result_key}",
        )
    with tabs[2]:
        _render_findings(findings)
    with tabs[3]:
        _render_requirements(plan)
    with tabs[4]:
        with st.container(horizontal=True, gap="small"):
            st.badge("AI-generated", color="orange")
            if trace.get("valid"):
                st.badge("Validated", color="green")
        render_records(
            test_output.get("test_cases", []),
            "尚无测试用例。",
            _TEST_CASE_COLUMNS,
            key=f"test-cases-{result_key}",
        )
    with tabs[5]:
        st.badge("Deterministic", color="blue")
        if trace.get("valid"):
            st.success(
                "Review → Finding → Requirement → TestCase 追溯链已通过校验。",
                icon=":material/verified:",
            )
        elif trace_output is None:
            st.info("追溯校验阶段尚未完成；不会假定结果已经通过。")
        else:
            st.error(
                "追溯链未通过，当前结果不可视为正式交付物。",
                icon=":material/link_off:",
            )
        rows = _traceability_rows(plan.get("requirements", []), test_output.get("test_cases", []))
        render_records(
            rows,
            "尚无可展示的证据链。",
            ("requirement_id", "finding_ids", "review_ids", "test_case_ids"),
            key=f"traceability-{result_key}",
        )
        render_records(
            trace.get("issues", []),
            "没有追溯问题。",
            _ISSUE_COLUMNS,
            key=f"traceability-issues-{result_key}",
        )
    with tabs[6]:
        event_rows = [
            {
                "time": event.created_at.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
                "stage": event.stage.value,
                "status": event.status.value,
                "message": event.message,
            }
            for event in reversed(events)
        ]
        render_records(
            event_rows,
            "尚无运行日志。",
            ("time", "stage", "status", "message"),
            key=f"events-{result_key}",
        )
        limitation_rows = [
            {
                "scope": "run",
                "entity_id": run.run_id,
                "limitation": limitation,
            }
            for limitation in run_limitations
        ] + [
            {
                "scope": "finding",
                "entity_id": finding.get("finding_id"),
                "limitation": limitation,
            }
            for finding in findings.get("findings", [])
            for limitation in finding.get("limitations", [])
        ]
        render_records(
            limitation_rows,
            "当前没有记录运行或 Finding 局限。",
            ("scope", "entity_id", "limitation"),
            key=f"limitations-{result_key}",
        )
        render_records(
            findings.get("report", {}).get("issues", []),
            "当前没有 Finding 校验问题。",
            _ISSUE_COLUMNS,
            key=f"finding-issues-{result_key}",
        )
