from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app_review_insights.errors import InputDataError
from app_review_insights.llm.recording import (
    RECORDING_MODE,
    input_fingerprint,
    load_recording,
    recording_key,
)
from app_review_insights.models import Review


def _review(review_id: str, content: str) -> Review:
    return Review(
        review_id=review_id,
        app_id="demo",
        content_original=content,
        rating=1,
        published_at=datetime(2026, 7, 1, 10, 0, tzinfo=UTC),
        source="sample",
    )


def _document(**overrides) -> dict:
    payload = {
        "mode": RECORDING_MODE,
        "is_live": False,
        "recorded_at": "2026-09-17T10:00:00Z",
        "model": "deepseek-chat",
        "input_fingerprint": "abc123",
        "entries": [],
    }
    payload.update(overrides)
    return payload


def test_recording_key_is_stable_and_length_fixed():
    once = recording_key("BatchAnalysisResult", "sys", "user")
    assert once == recording_key("BatchAnalysisResult", "sys", "user")
    assert len(once) == 16


def test_recording_key_changes_with_every_component():
    base = recording_key("BatchAnalysisResult", "sys", "user")
    assert base != recording_key("ConsolidationResult", "sys", "user")
    assert base != recording_key("BatchAnalysisResult", "sys2", "user")
    assert base != recording_key("BatchAnalysisResult", "sys", "user2")


def test_recording_key_does_not_collide_across_field_boundaries():
    # 三段用 NUL 分隔：("ab", "c") 不能与 ("a", "bc") 撞成同一个键
    assert recording_key("S", "ab", "c") != recording_key("S", "a", "bc")


def test_input_fingerprint_ignores_order_but_not_content():
    first, second = _review("r-1", "免费内容变少了"), _review("r-2", "试用期直接扣费")
    assert input_fingerprint([first, second]) == input_fingerprint([second, first])
    assert input_fingerprint([first, second]) != input_fingerprint([first])
    assert input_fingerprint([first]) != input_fingerprint([_review("r-1", "改过的正文")])


def test_load_recording_accepts_valid_document(tmp_path: Path):
    path = tmp_path / "rec.json"
    path.write_text(json.dumps(_document(), ensure_ascii=False), encoding="utf-8")
    document = load_recording(path)
    assert document.mode == RECORDING_MODE
    assert document.is_live is False
    assert document.entries == []


def test_load_recording_rejects_wrong_mode(tmp_path: Path):
    path = tmp_path / "rec.json"
    path.write_text(json.dumps(_document(mode="live_run")), encoding="utf-8")
    with pytest.raises(InputDataError, match="mode"):
        load_recording(path)


def test_load_recording_rejects_live_true(tmp_path: Path):
    path = tmp_path / "rec.json"
    path.write_text(json.dumps(_document(is_live=True)), encoding="utf-8")
    with pytest.raises(InputDataError, match="is_live"):
        load_recording(path)


def test_load_recording_rejects_malformed_json(tmp_path: Path):
    path = tmp_path / "rec.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(InputDataError, match="JSON"):
        load_recording(path)
