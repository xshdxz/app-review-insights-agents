"""结构化日志与关联 ID。

部署之后排查问题全靠日志。文本日志里没有 run_id，一条报错无法定位到具体哪次运行；
JSON 日志可以直接被采集器索引，按 run_id 关联出整条链路。

用法：在进程入口调用一次 :func:`configure_logging`；
`LOG_FORMAT=json` 时输出 JSON Lines，否则保持人类可读的文本格式。
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from app_review_insights.llm.usage import current_run_id, current_stage


class RunContextFilter(logging.Filter):
    """把 contextvar 里的 run_id / stage 注入每条日志记录。

    这样流水线内部任意深度的日志都自动带上关联 ID，不需要逐处传参。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = current_run_id.get()
        record.stage = current_stage.get()
        return True


class JsonFormatter(logging.Formatter):
    """JSON Lines 格式：一行一条，便于采集器索引。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "run_id": getattr(record, "run_id", None),
            "stage": getattr(record, "stage", None),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # ensure_ascii=False 保留中文；separators 去掉空格，保证单行紧凑
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(level: str = "INFO", fmt: str = "text") -> None:
    """配置根 logger；重复调用会替换已有 handler，不会重复输出。"""
    handler = logging.StreamHandler(sys.stderr)
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(message)s"))
    handler.addFilter(RunContextFilter())

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
