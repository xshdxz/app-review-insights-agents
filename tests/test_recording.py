from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app_review_insights.errors import InputDataError, RecoverableModelError
from app_review_insights.llm.recording import (
    RECORDING_MODE,
    RecordingDocument,
    RecordingProvider,
    ReplayMissError,
    ReplayProvider,
    input_fingerprint,
    load_recording,
    recording_key,
    verify_input_fingerprint,
    write_recording,
)
from app_review_insights.llm.schemas import BatchAnalysisResult, RequirementPlanResult
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


def _document_with_fingerprint(reviews: list[Review]) -> RecordingDocument:
    return RecordingDocument.model_validate(_document(input_fingerprint=input_fingerprint(reviews)))


def test_input_fingerprint_check_accepts_the_recorded_input():
    reviews = [_review("r-1", "免费内容变少了"), _review("r-2", "试用期直接扣费")]
    # 顺序无关：回放方与录制方拿到同一份输入即可，不要求逐字同一顺序
    verify_input_fingerprint(_document_with_fingerprint(reviews), list(reversed(reviews)))


def test_input_fingerprint_check_rejects_a_different_input():
    recorded = [_review("r-1", "免费内容变少了")]
    other = [_review("r-1", "免费内容变少了"), _review("r-2", "多出来的一条")]
    with pytest.raises(InputDataError) as excinfo:
        verify_input_fingerprint(_document_with_fingerprint(recorded), other)

    message = str(excinfo.value)
    # 消息要能让人知道怎么办：说明「不是同一份」、给出两边指纹、指出下一步
    assert "不是同一份" in message
    assert input_fingerprint(recorded) in message
    assert input_fingerprint(other) in message
    assert "record_demo.py" in message


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


def test_load_recording_rejects_missing_file(tmp_path: Path):
    # 文件不存在与「读不出来」分开报：前者是配置指错了路径，后者是文件本身有问题，
    # 提示不同用户才知道该去改哪一个。
    with pytest.raises(InputDataError, match="录制文件不存在"):
        load_recording(tmp_path / "not-there.json")


def test_load_recording_rejects_json_that_is_not_an_object(tmp_path: Path):
    # 合法 JSON 但不是对象（数组/标量）：直接说清期望的形状，而不是抛 pydantic 的英文报错
    path = tmp_path / "rec.json"
    path.write_text(json.dumps([{"mode": RECORDING_MODE}]), encoding="utf-8")
    with pytest.raises(InputDataError, match="JSON 对象"):
        load_recording(path)


def test_load_recording_rejects_malformed_json(tmp_path: Path):
    path = tmp_path / "rec.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(InputDataError, match="JSON"):
        load_recording(path)


def test_load_recording_rejects_directory_path(tmp_path: Path):
    directory = tmp_path / "recording-dir"
    directory.mkdir()
    with pytest.raises(InputDataError, match="无法读取"):
        load_recording(directory)


def test_load_recording_rejects_non_utf8_file(tmp_path: Path):
    # Windows 记事本存成 "Unicode" 编码就是这么个文件：不是合法 UTF-8
    path = tmp_path / "rec.json"
    path.write_bytes(b"\xff\xfe not utf8")
    with pytest.raises(InputDataError, match="无法读取"):
        load_recording(path)


def _provider_with_one_entry() -> ReplayProvider:
    """构造只含一次 BatchAnalysisResult 调用的回放 provider。"""
    system, user = "系统提示", "用户提示"
    document = RecordingDocument.model_validate(
        _document(
            entries=[
                {
                    "key": recording_key(BatchAnalysisResult.__name__, system, user),
                    "schema_name": BatchAnalysisResult.__name__,
                    "request": {"system": system, "user": user},
                    "response": {
                        "findings": [],
                        "review_summaries": [{"review_id": "r1", "summary_zh": "试用期扣费不透明"}],
                    },
                }
            ]
        )
    )
    return ReplayProvider(document)


def test_replay_returns_response_validated_against_requested_schema():
    provider = _provider_with_one_entry()
    result = provider.generate("系统提示", "用户提示", BatchAnalysisResult)
    assert isinstance(result, BatchAnalysisResult)
    # 只断言类型是不够的：BatchAnalysisResult 的 findings 有默认值、Schema 未设 extra 策略，
    # 一个把录制内容整个丢掉、直接返回空结果的实现同样能通过 isinstance。
    # 必须钉住录制里的具体内容确实抵达了调用方。
    assert result.review_summaries[0].review_id == "r1"
    assert result.review_summaries[0].summary_zh == "试用期扣费不透明"


def test_replay_exposes_recorded_model_name():
    assert _provider_with_one_entry().model == "deepseek-chat"


def test_replay_miss_raises_recoverable_error():
    provider = _provider_with_one_entry()
    with pytest.raises(ReplayMissError) as excinfo:
        provider.generate("系统提示", "换过的用户提示", BatchAnalysisResult)
    assert isinstance(excinfo.value, RecoverableModelError)
    assert "回放未命中" in str(excinfo.value)


def test_replay_rejects_recording_from_a_different_schema():
    provider = _provider_with_one_entry()
    with pytest.raises(ReplayMissError, match="Schema"):
        provider.generate("系统提示", "用户提示", RequirementPlanResult)


def test_replay_rejects_response_that_does_not_match_the_schema():
    """键命中了、但录下的内容不符合本次请求的 Schema：必须报未命中而不是把脏数据放过去。

    与 test_replay_rejects_recording_from_a_different_schema 的区别：那条走的是
    「同一请求录的是别的 Schema」分支，这条走的是「键命中但 response 校验失败」分支。
    后果同样严重——放过去就等于把未经校验的内容当成模型输出交给流水线。
    """
    system, user = "系统提示", "用户提示"
    document = RecordingDocument.model_validate(
        _document(
            entries=[
                {
                    "key": recording_key(BatchAnalysisResult.__name__, system, user),
                    "schema_name": BatchAnalysisResult.__name__,
                    "request": {"system": system, "user": user},
                    # findings 的类型不对：Schema 校验必然失败
                    "response": {"findings": "这不是一个列表"},
                }
            ]
        )
    )
    with pytest.raises(ReplayMissError, match="不匹配"):
        ReplayProvider(document).generate(system, user, BatchAnalysisResult)


class _StubInner:
    """替身 provider：按顺序返回预置响应，不做任何外部调用。"""

    def __init__(self, results: list[Any]) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, str]] = []

    def generate(self, system_prompt: str, user_prompt: str, schema):
        self.calls.append((system_prompt, user_prompt))
        return schema.model_validate(self.results.pop(0))


def test_recording_provider_passes_through_result(tmp_path: Path):
    inner = _StubInner([{"findings": []}])
    provider = RecordingProvider(
        inner, tmp_path / "rec.json", input_fingerprint="fp1", model="deepseek-chat"
    )
    result = provider.generate("s", "u", BatchAnalysisResult)
    assert isinstance(result, BatchAnalysisResult)
    assert inner.calls == [("s", "u")]


def test_recording_provider_writes_loadable_document(tmp_path: Path):
    destination = tmp_path / "rec.json"
    inner = _StubInner([{"findings": []}])
    provider = RecordingProvider(inner, destination, input_fingerprint="fp1", model="deepseek-chat")
    provider.generate("s", "u", BatchAnalysisResult)

    document = load_recording(destination)
    assert document.input_fingerprint == "fp1"
    assert document.model == "deepseek-chat"
    assert len(document.entries) == 1
    assert document.entries[0].schema_name == BatchAnalysisResult.__name__


def test_recording_provider_output_can_be_replayed(tmp_path: Path):
    destination = tmp_path / "rec.json"
    inner = _StubInner([{"findings": []}])
    RecordingProvider(inner, destination, input_fingerprint="fp1", model="deepseek-chat").generate(
        "s", "u", BatchAnalysisResult
    )

    replayed = ReplayProvider(load_recording(destination)).generate("s", "u", BatchAnalysisResult)
    assert isinstance(replayed, BatchAnalysisResult)


def test_recording_provider_does_not_swallow_inner_failure(tmp_path: Path):
    class _Boom:
        def generate(self, system_prompt, user_prompt, schema):
            raise RuntimeError("模型调用失败")

    provider = RecordingProvider(_Boom(), tmp_path / "rec.json", input_fingerprint="fp1", model="m")
    with pytest.raises(RuntimeError, match="模型调用失败"):
        provider.generate("s", "u", BatchAnalysisResult)


def test_recording_provider_failure_to_write_does_not_break_the_call(tmp_path: Path):
    # 用一个「是文件不是目录」的路径让写盘必定失败
    blocked = tmp_path / "blocked"
    blocked.write_text("我是文件不是目录", encoding="utf-8")
    inner = _StubInner([{"findings": []}])
    provider = RecordingProvider(inner, blocked / "rec.json", input_fingerprint="fp1", model="m")
    result = provider.generate("s", "u", BatchAnalysisResult)
    assert isinstance(result, BatchAnalysisResult)


def test_write_recording_replaces_previous_content_atomically(tmp_path: Path):
    destination = tmp_path / "rec.json"
    destination.write_text("旧内容", encoding="utf-8")
    write_recording(
        RecordingDocument.model_validate(_document(input_fingerprint="fp2")), destination
    )
    assert load_recording(destination).input_fingerprint == "fp2"
    assert not list(tmp_path.glob("*.tmp")), "临时文件必须被替换掉"
