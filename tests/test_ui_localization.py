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
