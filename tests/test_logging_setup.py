"""结构化日志与关联 ID。

部署之后排查问题全靠日志。文本日志里没有 run_id，一条报错无法定位到具体哪次运行；
JSON 日志可以直接被采集器索引，按 run_id 关联出整条链路。
"""

from __future__ import annotations

import json
import logging

import pytest

from app_review_insights.llm.usage import current_run_id, current_stage
from app_review_insights.logging_setup import (
    JsonFormatter,
    RunContextFilter,
    configure_logging,
)


def _record(**extra) -> logging.LogRecord:
    record = logging.LogRecord(
        name="ari-test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="任务失败 %s",
        args=("采集源",),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


# ── 关联 ID 注入 ─────────────────────────────────────────────────────────────


def test_context_filter_injects_run_id_and_stage():
    token_run = current_run_id.set("run-1")
    token_stage = current_stage.set("analyze_batches")
    try:
        record = _record()
        RunContextFilter().filter(record)
    finally:
        current_run_id.reset(token_run)
        current_stage.reset(token_stage)

    assert record.run_id == "run-1"
    assert record.stage == "analyze_batches"


def test_context_filter_tolerates_missing_context():
    record = _record()
    RunContextFilter().filter(record)

    assert record.run_id is None
    assert record.stage is None


def test_context_filter_never_drops_records():
    assert RunContextFilter().filter(_record()) is True


# ── JSON 输出 ────────────────────────────────────────────────────────────────


def test_json_formatter_emits_parseable_line_with_context():
    token = current_run_id.set("run-42")
    try:
        record = _record()
        RunContextFilter().filter(record)
        payload = json.loads(JsonFormatter().format(record))
    finally:
        current_run_id.reset(token)

    assert payload["level"] == "INFO"
    assert payload["logger"] == "ari-test"
    assert payload["message"] == "任务失败 采集源"
    assert payload["run_id"] == "run-42"
    assert "timestamp" in payload


def test_json_formatter_includes_exception_when_present():
    try:
        raise RuntimeError("采集源返回 0 条评论")
    except RuntimeError:
        import sys

        record = _record()
        record.exc_info = sys.exc_info()

    payload = json.loads(JsonFormatter().format(record))

    assert "RuntimeError" in payload["exception"]
    assert "采集源返回 0 条评论" in payload["exception"]


def test_json_formatter_output_is_single_line():
    try:
        raise ValueError("多行\n异常")
    except ValueError:
        import sys

        record = _record()
        record.exc_info = sys.exc_info()

    assert "\n" not in JsonFormatter().format(record)


# ── 配置入口 ─────────────────────────────────────────────────────────────────


@pytest.fixture
def restore_root_logging():
    root = logging.getLogger()
    saved = root.handlers[:]
    saved_level = root.level
    yield root
    root.handlers[:] = saved
    root.setLevel(saved_level)


def test_configure_logging_uses_json_formatter(restore_root_logging):
    configure_logging(level="DEBUG", fmt="json")

    root = restore_root_logging
    assert root.level == logging.DEBUG
    formatters = [handler.formatter for handler in root.handlers]
    assert any(isinstance(formatter, JsonFormatter) for formatter in formatters)
    assert all(
        any(isinstance(f, RunContextFilter) for f in handler.filters) for handler in root.handlers
    )


def test_configure_logging_supports_plain_text(restore_root_logging):
    configure_logging(level="INFO", fmt="text")

    root = restore_root_logging
    assert not any(isinstance(handler.formatter, JsonFormatter) for handler in root.handlers)


def test_configure_logging_replaces_previous_handlers(restore_root_logging):
    configure_logging(level="INFO", fmt="json")
    first = len(restore_root_logging.handlers)
    configure_logging(level="INFO", fmt="json")

    assert len(restore_root_logging.handlers) == first
