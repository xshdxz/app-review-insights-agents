from app_review_insights.ui import components
from app_review_insights.ui.components import (
    COMPLEXITY_LABELS,
    PROVENANCE_LABELS,
    TABLE_COLUMN_LABELS,
    TARGET_VERSION_LABELS,
    _display_records_frame,
    format_event_message,
)


def test_provenance_labels_are_chinese():
    assert PROVENANCE_LABELS == {
        "deterministic": "程序计算",
        "ai_generated": "模型生成",
        "validated": "已校验",
        "assumption": "假设/待确认",
    }


def test_requirement_controlled_values_are_chinese():
    assert COMPLEXITY_LABELS == {"low": "低", "medium": "中", "high": "高"}
    assert TARGET_VERSION_LABELS["Future"] == "待规划"


def test_format_event_message_translates_known_events():
    assert format_event_message("Analysis run created") == "分析任务已创建"
    assert format_event_message("Review collection completed") == "评论采集或导入已完成"
    assert format_event_message("Batch analysis completed") == "一批评论分析完成"
    assert format_event_message("Analysis run completed") == "分析任务已完成"


def test_format_event_message_translates_stage_names():
    assert format_event_message("Stage started: collect") == "开始阶段：采集或导入"
    assert format_event_message("Stage started: analyze_batches") == "开始阶段：分批语义分析"


def test_format_event_message_preserves_unknown_diagnostics():
    assert format_event_message("Provider returned a new diagnostic") == (
        "Provider returned a new diagnostic"
    )


def test_display_records_frame_localizes_table_headers_and_controlled_values():
    frame = _display_records_frame(
        [
            {
                "time": "2026-08-15 10:00:00",
                "stage": "collect",
                "status": "running",
                "case_type": "boundary",
                "source": "apple-rss:us",
                "scope": "finding",
                "rule": "finding_support_threshold",
                "message": "支持证据不足，Finding 标记为 Assumption。",
                "revision_action": "reject_finding",
                "content_original": "Keep the original review text.",
            }
        ],
        (
            "time",
            "stage",
            "status",
            "case_type",
            "source",
            "scope",
            "rule",
            "message",
            "revision_action",
            "content_original",
        ),
    )

    assert list(frame.columns) == [
        TABLE_COLUMN_LABELS["time"],
        TABLE_COLUMN_LABELS["stage"],
        TABLE_COLUMN_LABELS["status"],
        TABLE_COLUMN_LABELS["case_type"],
        TABLE_COLUMN_LABELS["source"],
        TABLE_COLUMN_LABELS["scope"],
        TABLE_COLUMN_LABELS["rule"],
        TABLE_COLUMN_LABELS["message"],
        TABLE_COLUMN_LABELS["revision_action"],
        TABLE_COLUMN_LABELS["content_original"],
    ]
    assert frame.iloc[0][TABLE_COLUMN_LABELS["stage"]] == "采集或导入"
    assert frame.iloc[0][TABLE_COLUMN_LABELS["status"]] == "运行中"
    assert frame.iloc[0][TABLE_COLUMN_LABELS["case_type"]] == "边界"
    assert frame.iloc[0][TABLE_COLUMN_LABELS["source"]] == "Apple RSS（美国区）"
    assert frame.iloc[0][TABLE_COLUMN_LABELS["scope"]] == "问题发现"
    assert frame.iloc[0][TABLE_COLUMN_LABELS["rule"]] == "问题发现支持证据阈值"
    assert frame.iloc[0][TABLE_COLUMN_LABELS["message"]] == (
        "支持证据不足，问题发现标记为“假设/待确认”。"
    )
    assert frame.iloc[0][TABLE_COLUMN_LABELS["revision_action"]] == "拒绝问题发现"
    assert frame.iloc[0][TABLE_COLUMN_LABELS["content_original"]] == (
        "Keep the original review text."
    )


def test_display_records_frame_keeps_empty_chinese_summary_column():
    frame = _display_records_frame(
        [
            {
                "review_id": "r-1",
                "content_original": "The renewal date is unclear.",
                "content_summary_zh": None,
            }
        ],
        ("review_id", "content_original", "content_summary_zh"),
    )

    assert list(frame.columns) == ["评论 ID", "评论原文", "中文摘要"]
    assert frame.iloc[0]["中文摘要"] == "待生成"


def test_finding_evidence_rows_align_original_summary_and_audit_reason():
    rows = components._finding_evidence_rows(
        {
            "supporting_review_ids": ["r-1"],
            "conflicting_review_ids": ["r-2"],
            "evidence_assessments": [
                {
                    "review_id": "r-1",
                    "role": "supporting",
                    "rationale_zh": "原文直接支持问题。",
                },
                {
                    "review_id": "r-2",
                    "role": "conflicting",
                    "rationale_zh": "原文表达相反体验。",
                },
            ],
        },
        [
            {
                "review_id": "r-1",
                "content_original": "The renewal date is unclear.",
                "content_summary_zh": "用户不清楚续费日期。",
            },
            {
                "review_id": "r-2",
                "content_original": "The renewal date is clear.",
                "content_summary_zh": None,
            },
        ],
    )

    assert rows == [
        {
            "评论 ID": "r-1",
            "证据角色": "支持",
            "评论原文": "The renewal date is unclear.",
            "中文摘要": "用户不清楚续费日期。",
            "复核理由": "原文直接支持问题。",
        },
        {
            "评论 ID": "r-2",
            "证据角色": "冲突",
            "评论原文": "The renewal date is clear.",
            "中文摘要": "待生成",
            "复核理由": "原文表达相反体验。",
        },
    ]


def test_finding_validation_states_are_separate_and_explicit():
    states = components._finding_validation_states(
        {
            "schema_validated": True,
            "reference_validated": False,
            "semantic_validated": False,
        }
    )

    assert states == [
        ("Schema 校验", "已通过", True),
        ("引用存在性", "未通过", False),
        ("证据语义", "待复核", False),
    ]


def test_requirement_metadata_separates_target_version_and_business_assumptions():
    metadata = components._requirement_display_metadata(
        {
            "target_version": "V1.0",
            "assumptions": ["需要确认现有订阅页是否支持灰度发布。"],
        }
    )

    assert metadata == {
        "target_version": "V1.0",
        "business_assumptions": ["需要确认现有订阅页是否支持灰度发布。"],
    }
