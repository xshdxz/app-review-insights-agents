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
    "audit_evidence": "证据语义复核",
    "validate_findings": "证据校验",
    "plan": "产品规划",
    "generate_tests": "生成测试",
    "validate_traceability": "追溯校验",
    "complete": "完成",
}

PROVENANCE_LABELS = {
    "deterministic": "程序计算",
    "ai_generated": "模型生成",
    "validated": "已校验",
    "assumption": "假设/待确认",
}

COMPLEXITY_LABELS = {
    "low": "低",
    "medium": "中",
    "high": "高",
}

TARGET_VERSION_LABELS = {
    "V1.0": "V1.0",
    "V1.1": "V1.1",
    "Future": "待规划",
}

EVIDENCE_ROLE_LABELS = {
    "supporting": "支持",
    "conflicting": "冲突",
    "irrelevant": "无关",
}

TABLE_COLUMN_LABELS = {
    "review_id": "评论 ID",
    "rating": "评分",
    "app_version": "App 版本",
    "published_at": "发布时间",
    "language": "语言",
    "content_original": "评论原文",
    "content_summary_zh": "中文摘要",
    "source": "数据来源",
    "test_case_id": "测试用例 ID",
    "requirement_id": "需求 ID",
    "title": "标题",
    "case_type": "用例类型",
    "preconditions": "前置条件",
    "steps": "测试步骤",
    "expected_result": "预期结果",
    "source_review_ids": "来源评论 ID",
    "severity": "严重程度",
    "entity_type": "对象类型",
    "entity_id": "对象 ID",
    "rule": "校验规则",
    "message": "说明",
    "revision_action": "修正建议",
    "finding_ids": "问题发现 ID",
    "review_ids": "评论 ID",
    "test_case_ids": "测试用例 ID",
    "time": "时间",
    "stage": "阶段",
    "status": "状态",
    "scope": "范围",
    "limitation": "局限说明",
}

_TABLE_VALUE_LABELS = {
    "stage": STAGE_LABELS,
    "status": STATUS_LABELS,
    "source": {
        "apple-rss:us": "Apple RSS（美国区）",
        "import:json": "JSON 导入",
        "import:csv": "CSV 导入",
        "json": "JSON 导入",
        "csv": "CSV 导入",
        "fixture": "样例数据",
    },
    "case_type": {
        "normal": "常规",
        "boundary": "边界",
        "exception": "异常",
        "regression": "回归",
    },
    "severity": {
        "info": "提示",
        "warning": "警告",
        "error": "错误",
        "critical": "严重",
    },
    "entity_type": {
        "review": "评论",
        "finding": "问题发现",
        "requirement": "产品需求",
        "test_case": "测试用例",
    },
    "scope": {
        "run": "运行",
        "finding": "问题发现",
    },
    "rule": {
        "entity_id_unique": "对象 ID 唯一",
        "review_reference_exists": "评论引用存在",
        "finding_has_support": "问题发现具有支持证据",
        "finding_support_threshold": "问题发现支持证据阈值",
        "evidence_audit_complete": "证据语义复核完整",
        "evidence_audit_reference_exists": "证据复核引用存在",
        "evidence_audit_unique": "证据复核引用唯一",
        "review_to_finding": "评论到问题发现",
        "finding_to_requirement": "问题发现到产品需求",
        "requirement_uses_eligible_finding": "产品需求仅使用合格问题发现",
        "requirement_reviews_inherit_findings": "产品需求继承问题发现的评论来源",
        "requirement_to_test_case": "产品需求到测试用例",
        "test_case_count_per_requirement": "每项需求的测试用例数量",
    },
    "revision_action": {
        "remove_invalid_references": "删除无效引用",
        "remove_unknown_audit_references": "删除未知复核引用",
        "deduplicate_audit_references": "去重证据复核引用",
        "audit_missing_references": "补充缺失证据复核",
        "reject_finding": "拒绝问题发现",
    },
}

_VALIDATION_MESSAGE_REPLACEMENTS = (
    ("Finding 标记为 Assumption", "问题发现标记为“假设/待确认”"),
    ("TestCase", "测试用例"),
    ("Requirement", "产品需求"),
    ("Finding", "问题发现"),
    ("test_case ID", "测试用例 ID"),
    ("requirement ID", "产品需求 ID"),
    ("finding ID", "问题发现 ID"),
    ("Assumption", "假设/待确认"),
)

_EVENT_MESSAGE_LABELS = {
    "Analysis run created": "分析任务已创建",
    "Analysis run resumed": "分析任务已恢复",
    "Review collection failed": "评论采集或导入失败",
    "Review collection completed": "评论采集或导入已完成",
    "Review cleaning completed": "评论清洗已完成",
    "Batch analysis paused for retry": "分批分析暂停，等待重试",
    "Batch analysis completed": "一批评论分析完成",
    "All review batches analyzed": "全部评论批次分析完成",
    "Finding consolidation completed": "问题发现归并已完成",
    "Finding evidence audit completed": "问题发现证据语义复核已完成",
    "Finding validation completed": "问题发现证据校验已完成",
    "Product planning completed": "产品规划已完成",
    "Test generation completed": "测试用例生成已完成",
    "Traceability validation completed": "证据链校验已完成",
    "Traceability validation found unresolved issues": "证据链校验发现未解决问题",
    "Model stage paused for retry": "模型阶段暂停，等待重试",
    "Analysis run completed": "分析任务已完成",
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


def _display_records_frame(
    records: Iterable[dict[str, Any]],
    allowed_columns: Iterable[str],
) -> pd.DataFrame:
    columns = list(allowed_columns)
    frame = _records_frame(records, columns)
    if "content_summary_zh" in columns:
        if "content_summary_zh" not in frame.columns:
            frame["content_summary_zh"] = "待生成"
        else:
            frame["content_summary_zh"] = frame["content_summary_zh"].map(
                lambda value: value if isinstance(value, str) and value.strip() else "待生成"
            )
        frame = frame[[column for column in columns if column in frame.columns]]
    for column, value_labels in _TABLE_VALUE_LABELS.items():
        if column not in frame.columns:
            continue
        labels = value_labels
        frame[column] = frame[column].map(
            lambda value, labels=labels: (
                labels.get(value, value) if isinstance(value, str) else value
            )
        )
    if "message" in frame.columns:
        frame["message"] = frame["message"].map(_localize_validation_message)
    return frame.rename(columns=TABLE_COLUMN_LABELS)


def _localize_validation_message(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    for original, localized in _VALIDATION_MESSAGE_REPLACEMENTS:
        value = value.replace(original, localized)
    return value


def _finding_evidence_rows(
    finding: Mapping[str, Any],
    reviews: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    review_index = {review.get("review_id"): review for review in reviews}
    assessment_index = {
        assessment.get("review_id"): assessment
        for assessment in finding.get("evidence_assessments", [])
    }
    supporting_ids = finding.get("supporting_review_ids", [])
    conflicting_ids = finding.get("conflicting_review_ids", [])
    cited_ids = list(dict.fromkeys(supporting_ids + conflicting_ids))
    rows = []
    for review_id in cited_ids:
        review = review_index.get(review_id, {})
        assessment = assessment_index.get(review_id, {})
        default_role = "supporting" if review_id in supporting_ids else "conflicting"
        summary = review.get("content_summary_zh")
        rows.append(
            {
                "评论 ID": review_id,
                "证据角色": EVIDENCE_ROLE_LABELS.get(
                    assessment.get("role", default_role),
                    assessment.get("role", default_role),
                ),
                "评论原文": review.get(
                    "content_original",
                    "未找到原评论（引用存在性校验未通过）。",
                ),
                "中文摘要": (summary if isinstance(summary, str) and summary.strip() else "待生成"),
                "复核理由": assessment.get(
                    "rationale_zh",
                    "尚未完成证据语义复核。",
                ),
            }
        )
    return rows


def _finding_validation_states(
    finding: Mapping[str, Any],
) -> list[tuple[str, str, bool]]:
    return [
        (
            "Schema 校验",
            "已通过" if finding.get("schema_validated", False) else "未通过",
            bool(finding.get("schema_validated", False)),
        ),
        (
            "引用存在性",
            "已通过" if finding.get("reference_validated", False) else "未通过",
            bool(finding.get("reference_validated", False)),
        ),
        (
            "证据语义",
            "已复核" if finding.get("semantic_validated", False) else "待复核",
            bool(finding.get("semantic_validated", False)),
        ),
    ]


def _requirement_display_metadata(requirement: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "target_version": TARGET_VERSION_LABELS.get(
            requirement.get("target_version", "Future"),
            "待规划",
        ),
        "business_assumptions": requirement.get("assumptions", []),
    }


def format_event_message(message: str) -> str:
    """Translate persisted pipeline events for display without changing stored data."""
    if message in _EVENT_MESSAGE_LABELS:
        return _EVENT_MESSAGE_LABELS[message]
    if message.startswith("Stage started: "):
        stage = message.removeprefix("Stage started: ")
        return f"开始阶段：{STAGE_LABELS.get(stage, stage)}"
    return message


def render_provenance_legend() -> None:
    with st.container(horizontal=True, gap="small"):
        st.badge(
            PROVENANCE_LABELS["deterministic"],
            icon=":material/functions:",
            color="blue",
            help="由程序规则直接计算，不依赖语言模型。",
        )
        st.badge(
            PROVENANCE_LABELS["ai_generated"],
            icon=":material/auto_awesome:",
            color="orange",
            help="由 DeepSeek 生成，必须结合证据与校验状态审阅。",
        )
        st.badge(
            PROVENANCE_LABELS["validated"],
            icon=":material/verified:",
            color="green",
            help="已通过当前阶段的确定性校验。",
        )
        st.badge(
            PROVENANCE_LABELS["assumption"],
            icon=":material/help:",
            color="gray",
            help="证据不足或仍需人工确认，不能视为确定事实。",
        )


def render_model_status(
    model_state: str,
    key_source: str | None = None,
) -> None:
    # 正常状态（已配置或已验证）不打扰用户：横幅只在模型不可用时才出现。
    if model_state in ("verified", "configured"):
        return
    if model_state == "disabled":
        st.warning(
            "模型状态：已显式禁用（MODEL_ENABLED=false）。"
            "档案库和历史结果仍可读取，实时开始与继续分析暂不可用。",
            icon=":material/toggle_off:",
        )
    else:
        st.warning(
            "模型状态：未配置 MODEL_API_KEY。档案库仍可读取，但实时开始与继续分析暂不可用。",
            icon=":material/key_off:",
        )
    if key_source:
        st.caption(
            f"密钥配置来源：{key_source}。PowerShell 中设置空环境变量不会屏蔽项目 `.env`；"
            "如需无模型演示，请设置 `MODEL_ENABLED=false`。"
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

        st.caption(f"运行 ID：`{run.run_id}`")
        current_stage = STAGE_LABELS.get(run.current_stage.value, run.current_stage.value)
        st.write(f"当前阶段：`{current_stage}`")
        if run.total_batches:
            st.write(f"批次检查点：`已完成 {run.current_batch} / {run.total_batches}`")
        else:
            st.write("当前批次：`尚未分批`")

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
                st.write(format_event_message(event.message))


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
        _display_records_frame(records, allowed_columns),
        hide_index=True,
        key=key,
    )


def _run_limitations(run: RunRecord, clean: dict[str, Any]) -> list[str]:
    stats = clean.get("stats", {})
    limitations = []
    if run.request.source_type == SourceType.ONLINE and "input_count" in stats:
        actual_count = int(stats["input_count"])
        if actual_count < run.request.review_limit:
            limitations.append(
                f"在线采集目标 {run.request.review_limit} 条，实际获得 {actual_count} 条；"
                "样本短缺会降低证据覆盖度。"
            )

    collision_count = int(stats.get("review_id_collisions", 0))
    if collision_count:
        limitations.append(
            f"发现 {collision_count} 个重复评论 ID 对应不同正文；已稳定重命名，"
            "后续证据链使用重命名后的唯一 ID。"
        )
    return limitations


def _render_overview(
    clean: dict[str, Any],
    findings: dict[str, Any],
    plan: dict[str, Any],
    test_output: dict[str, Any],
    run_limitations: list[str],
) -> None:
    st.badge(PROVENANCE_LABELS["deterministic"], icon=":material/functions:", color="blue")
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

    st.badge(PROVENANCE_LABELS["ai_generated"], icon=":material/auto_awesome:", color="orange")
    with st.container(horizontal=True):
        st.metric("问题发现", len(findings.get("findings", [])), border=True)
        st.metric("产品需求（PRD）", len(plan.get("requirements", [])), border=True)
        st.metric("测试用例", len(test_output.get("test_cases", [])), border=True)


def _render_findings(
    findings_output: dict[str, Any],
    reviews: list[dict[str, Any]],
) -> None:
    findings = findings_output.get("findings", [])
    if not findings:
        st.info("尚无问题发现。模型暂停时，已完成阶段的结果仍会保留。")
        return

    for finding in findings:
        status = finding.get("evidence_status", "assumption")
        status_label = (
            PROVENANCE_LABELS["validated"]
            if status == "validated"
            else PROVENANCE_LABELS["assumption"]
        )
        status_color = "green" if status == "validated" else "gray"
        with st.expander(
            f"{finding.get('finding_id', '问题发现')} · {finding.get('title', '未命名发现')}",
            icon=":material/article:",
        ):
            with st.container(horizontal=True, gap="small"):
                st.badge(PROVENANCE_LABELS["ai_generated"], color="orange")
                st.badge(status_label, color=status_color)
            with st.container(horizontal=True, gap="small"):
                for label, state, _ in _finding_validation_states(finding):
                    st.markdown(f"**{label}**：{state}")
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
            st.markdown(f"**模型归纳说明**：{finding.get('model_reasoning_summary', '—')}")
            st.markdown("**证据详情**")
            evidence_rows = _finding_evidence_rows(finding, reviews)
            if evidence_rows:
                st.dataframe(pd.DataFrame(evidence_rows), hide_index=True)
            else:
                st.warning("没有可展示的有效证据；该问题发现不应进入正式规划。")
            limitations = finding.get("limitations", [])
            st.markdown("**局限说明**")
            if limitations:
                st.warning("\n".join(f"- {item}" for item in limitations))
            else:
                st.caption("当前未记录额外局限。")


def _render_requirements(plan_output: dict[str, Any]) -> None:
    if plan_output.get("quantity_notice"):
        st.info(plan_output["quantity_notice"], icon=":material/info:")
    requirements = plan_output.get("requirements", [])
    if not requirements:
        st.info("尚无 PRD 需求。")
        return

    for requirement in requirements:
        metadata = _requirement_display_metadata(requirement)
        target_version = metadata["target_version"]
        complexity = COMPLEXITY_LABELS.get(
            requirement.get("complexity", ""),
            requirement.get("complexity", "—"),
        )
        with st.expander(
            f"{requirement.get('requirement_id', 'REQ')} · "
            f"{target_version} · "
            f"{requirement.get('title', '未命名需求')}",
            icon=":material/description:",
        ):
            with st.container(horizontal=True, gap="small"):
                st.badge(PROVENANCE_LABELS["ai_generated"], color="orange")
                if requirement.get("assumptions"):
                    st.badge(PROVENANCE_LABELS["assumption"], color="gray")
            st.markdown(f"**用户问题**：{requirement.get('user_problem', '—')}")
            st.markdown(f"**目标**：{requirement.get('objective', '—')}")
            st.markdown(f"**目标版本**：{target_version}")
            st.markdown(
                f"**优先级分**：{requirement.get('priority_score', 0)} · "
                f"**影响**：{requirement.get('impact', '—')} · "
                f"**复杂度**：{complexity}"
            )
            fields = (
                ("范围", "scope"),
                ("非目标", "non_goals"),
                ("功能规则", "functional_rules"),
                ("边界情况", "edge_cases"),
                ("验收标准", "acceptance_criteria"),
                ("成功指标", "success_metrics"),
                ("业务假设", "assumptions"),
            )
            for label, field in fields:
                values = (
                    metadata["business_assumptions"]
                    if field == "assumptions"
                    else requirement.get(field, [])
                )
                if values:
                    st.markdown(f"**{label}**")
                    st.markdown("\n".join(f"- {value}" for value in values))
            st.caption(
                "问题发现："
                + ", ".join(requirement.get("finding_ids", []))
                + " · 评论："
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
            ":material/lightbulb: 问题发现",
            ":material/description: 产品需求（PRD）",
            ":material/checklist: 测试用例",
            ":material/account_tree: 证据链",
            ":material/receipt_long: 日志与限制",
        ]
    )
    with tabs[0]:
        _render_overview(clean, findings, plan, test_output, run_limitations)
    with tabs[1]:
        st.caption(
            f"{PROVENANCE_LABELS['deterministic']} · 原始评论文本保留为证据，中文摘要不能替代原文。"
        )
        render_records(
            clean.get("reviews", []),
            "尚无清洗评论。",
            _REVIEW_COLUMNS,
            key=f"reviews-{result_key}",
        )
    with tabs[2]:
        _render_findings(findings, clean.get("reviews", []))
    with tabs[3]:
        _render_requirements(plan)
    with tabs[4]:
        with st.container(horizontal=True, gap="small"):
            st.badge(PROVENANCE_LABELS["ai_generated"], color="orange")
            if trace.get("valid"):
                st.badge(PROVENANCE_LABELS["validated"], color="green")
        render_records(
            test_output.get("test_cases", []),
            "尚无测试用例。",
            _TEST_CASE_COLUMNS,
            key=f"test-cases-{result_key}",
        )
    with tabs[5]:
        st.badge(PROVENANCE_LABELS["deterministic"], color="blue")
        if trace.get("valid"):
            st.success(
                "评论 → 问题发现 → 产品需求 → 测试用例追溯链已通过校验。",
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
                "stage": STAGE_LABELS.get(event.stage.value, event.stage.value),
                "status": STATUS_LABELS.get(event.status.value, event.status.value),
                "message": format_event_message(event.message),
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
            "当前没有记录运行或问题发现局限。",
            ("scope", "entity_id", "limitation"),
            key=f"limitations-{result_key}",
        )
        render_records(
            findings.get("report", {}).get("issues", []),
            "当前没有问题发现校验问题。",
            _ISSUE_COLUMNS,
            key=f"finding-issues-{result_key}",
        )
