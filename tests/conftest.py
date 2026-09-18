from datetime import UTC, datetime
from pathlib import Path

import pytest

from app_review_insights.llm.recording import (
    RECORDING_MODE,
    RecordingDocument,
    write_recording,
)


class SchemaFakeProvider:
    """Returns pre-built dicts validated against the requested schema."""

    def __init__(self, responses):
        self.responses = list(responses)

    def generate(self, system_prompt, user_prompt, schema):
        value = self.responses.pop(0)
        return schema.model_validate(value)


@pytest.fixture
def fake_provider_factory():
    return SchemaFakeProvider


def _recording_at(path: Path) -> Path:
    """写一份最小合法录制件到 path 并返回该路径（演示模式用例的公共前置）。

    放在 conftest 而不是某个测试模块里：tests/test_demo_mode.py 与
    tests/test_url_params.py 都要用它，从另一测试模块导入会让测试模块互相依赖。
    """
    write_recording(
        RecordingDocument(
            mode=RECORDING_MODE,
            is_live=False,
            recorded_at=datetime(2026, 9, 17, tzinfo=UTC),
            model="deepseek-chat",
            input_fingerprint="fp",
            entries=[],
        ),
        path,
    )
    return path
