# 演示模式与模型层录制回放 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让无模型密钥的部署也能以回放方式跑完完整流水线，同时把「模型输出可复现」变成可复用能力。

**Architecture:** 在 provider 边界加一层录制/回放。录制把每次 `generate` 的「请求 → 响应」按 `(Schema 名, system, user)` 的指纹存盘；回放按同一指纹取回，不发起任何外部调用。provider 的选择收在 `factory.build_pipeline_services` 这个唯一装配点，调用方无感。回放未命中复用既有的 `RecoverableModelError` 失败语义，停在检查点、可续跑。

**Tech Stack:** Python 3.11+、Pydantic v2、Streamlit 1.64、pytest、Ruff。

**Spec:** `docs/superpowers/specs/2026-09-17-demo-replay-design.md`

## Global Constraints

- 全部测试离线，绝不调用真实模型；需要模型的地方一律注入替身。
- 行宽 100；每个 Task 结束时 `ruff check .` 与 `ruff format --check .` 必须双绿。
- 新增配置必须同步三处：`src/app_review_insights/config.py`、`.env.example`、`README.md` 的配置表。缺一处算未完成。
- 中文优先：docstring、UI 文案、错误消息一律中文；标识符英文。
- 异常只用 `errors.py` 里已有的类，新增异常必须是其中某个类的子类。
- 测试数量只增不减（起点 459）。提交前用 `python -m pytest -q` 全量跑一遍。
- 文案与提交信息使用中性产品语言，只描述系统行为与取舍。
- 不要修改 `storage/migrations.py` 中已发布的迁移脚本。

---

## 文件职责

| 文件 | 职责 |
|---|---|
| `src/app_review_insights/llm/recording.py`（新增） | 录制文档格式、键推导、输入指纹、`RecordingProvider`、`ReplayProvider`、`ReplayMissError` |
| `src/app_review_insights/config.py` | 新增 `DEMO_MODE` / `MODEL_RECORD_PATH` / `DEMO_REPLAY_PATH` 与 `demo_replay_active` |
| `src/app_review_insights/factory.py` | 按模式选择 provider（唯一装配点） |
| `src/app_review_insights/ui/main.py` | 演示模式输入锁定、界面标注、URL 参数写入策略 |
| `scripts/record_demo.py`（新增） | 用真实模型跑一次样例分析并产出录制文件 |
| `tests/test_recording.py`（新增） | 录制/回放/格式校验 |
| `tests/test_record_demo.py`（新增） | 录制脚本（用替身 provider） |
| `tests/test_demo_mode.py`（新增） | 演示模式界面行为 |
| `tests/test_url_params.py`（新增） | URL 参数写入策略 |
| `docs/deploy-streamlit-cloud.md`、`README.md`、`.env.example`、`CHANGELOG.md` | 文档与配置表 |

---

### Task 1: 录制文档格式、键与输入指纹

**Files:**
- Create: `src/app_review_insights/llm/recording.py`
- Test: `tests/test_recording.py`

**Interfaces:**
- Consumes: `app_review_insights.models.Review`、`app_review_insights.errors.InputDataError`
- Produces:
  - `RECORDING_MODE: str`（值为 `recorded_live_run`）
  - `recording_key(schema_name: str, system_prompt: str, user_prompt: str) -> str`
  - `input_fingerprint(reviews: Sequence[Review]) -> str`
  - `RecordingDocument`（字段 `mode` / `is_live` / `recorded_at` / `model` / `input_fingerprint` / `entries`）
  - `RecordingEntry`（字段 `key` / `schema_name` / `request` / `response`）
  - `RecordingRequest`（字段 `system` / `user`）
  - `load_recording(path: Path | str) -> RecordingDocument`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_recording.py
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_recording.py -q`

Expected: FAIL，`ModuleNotFoundError: No module named 'app_review_insights.llm.recording'`

- [ ] **Step 3: 实现 recording.py**

```python
"""模型层录制与回放。

LLM 应用的输出不可复现：同一个输入，换一次调用就可能得到不同结论，测试因此只能
依赖手写假响应，与真实模型的输出分布长期脱节。这里在 provider 边界做一层录制——
把每次 generate 的「请求 → 响应」按请求指纹存下来，回放时按同一指纹取回，
不发起任何外部调用。

两条用途：
1. 离线演示：没有密钥的部署靠回放跑完整流水线；
2. 可复现回归：测试可以跑真实录制，而不是手写响应。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, Field, ValidationError, model_validator

from app_review_insights.errors import InputDataError, RecoverableModelError
from app_review_insights.models import Review

logger = logging.getLogger("ari-llm")

T = TypeVar("T", bound=BaseModel)

#: 录制文件的模式标记。与 storage/cache.py 的 historical_cache_demo 同一套约定：
#: 离线产物必须显式标注，且永远不是实时结果。
RECORDING_MODE = "recorded_live_run"

#: 请求三段之间的分隔符，避免「前一段结尾拼上后一段开头」撞出同一个键。
_FIELD_SEP = chr(0)


def recording_key(schema_name: str, system_prompt: str, user_prompt: str) -> str:
    """按请求内容推导录制键。"""
    payload = _FIELD_SEP.join((schema_name, system_prompt, user_prompt))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def input_fingerprint(reviews: Sequence[Review]) -> str:
    """输入评论集指纹，用于回放前发现「录制与当前输入不是同一份」。

    取排序后的 (review_id, content_original)，因此与评论顺序无关，但增删一条评论
    或改动一条正文都会换指纹。
    """
    parts = sorted(f"{review.review_id}{chr(31)}{review.content_original}" for review in reviews)
    return hashlib.sha256(chr(30).join(parts).encode("utf-8")).hexdigest()[:16]


class RecordingRequest(BaseModel):
    system: str
    user: str


class RecordingEntry(BaseModel):
    key: str
    schema_name: str
    request: RecordingRequest
    response: dict[str, Any]


class RecordingDocument(BaseModel):
    mode: str
    is_live: bool
    recorded_at: datetime
    model: str
    input_fingerprint: str
    entries: list[RecordingEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def must_be_labeled_non_live(self) -> RecordingDocument:
        if self.mode != RECORDING_MODE:
            raise ValueError(
                f"录制文件的 mode 必须是 {RECORDING_MODE!r}，实际为 {self.mode!r}；"
                "离线产物不得冒充实时结果。"
            )
        if self.is_live is not False:
            raise ValueError("录制文件的 is_live 必须是 false；离线产物不得冒充实时结果。")
        return self


def load_recording(path: Path | str) -> RecordingDocument:
    """读取并校验录制文件；标注不正确就拒绝加载。

    抛 InputDataError 而不是 RecoverableModelError：文件缺失或标错属于配置与素材
    问题，应当在运行开始前暴露，重试没有意义。
    """
    target = Path(path)
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise InputDataError(f"录制文件不存在：{target}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InputDataError(f"录制文件不是合法 JSON：{target}") from exc
    if not isinstance(payload, dict):
        raise InputDataError(f"录制文件必须是 JSON 对象：{target}")
    try:
        return RecordingDocument.model_validate(payload)
    except ValidationError as exc:
        first = exc.errors()[0]
        message = first.get("msg", "校验失败")
        raise InputDataError(f"录制文件校验失败：{target}（{message}）") from exc
```

> **执行偏差（Task 1 已按此落地）**：上面的导入区列了 `os`、`UTC`、`TypeVar`、
> `RecoverableModelError`，但它们要到 Task 2、Task 3 才用得上。按 R1 裁定（每个 Task
> 结束必须 ruff 双绿，未使用的导入过不了），**Task 1 只导入了自己用得到的名字**，
> 上面这四个名字以及模块级的 `T = TypeVar("T", bound=BaseModel)` 均未落地。
> 因此 Task 2 与 Task 3 必须各自补齐自己用到的导入——见各自 Task 末尾的说明。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_recording.py -q`

Expected: PASS（9 项）

- [ ] **Step 5: 提交**

```bash
git add src/app_review_insights/llm/recording.py tests/test_recording.py
git commit -m "feat: 录制文档格式、请求键与输入指纹"
```

---

### Task 2: 回放 provider

**Files:**
- Modify: `src/app_review_insights/llm/recording.py`（追加）
- Test: `tests/test_recording.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `RecordingDocument`、`recording_key`、`RECORDING_MODE`
- Produces:
  - `ReplayMissError`（`RecoverableModelError` 的子类）
  - `ReplayProvider(document: RecordingDocument)`，带 `.model: str` 与 `.generate(system_prompt, user_prompt, schema) -> T`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_recording.py（追加）
from app_review_insights.llm.recording import (
    RecordingDocument,
    ReplayMissError,
    ReplayProvider,
)
from app_review_insights.llm.schemas import BatchAnalysisResult, RequirementPlanResult


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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_recording.py -q`

Expected: FAIL，`ImportError: cannot import name 'ReplayMissError'`

- [ ] **Step 3: 实现 ReplayProvider**

```python
# 追加到 src/app_review_insights/llm/recording.py
class ReplayMissError(RecoverableModelError):
    """回放未命中：录制里没有这次请求。

    继承 RecoverableModelError 是为了复用既有的失败语义——流水线停在检查点、
    状态转为 waiting_for_model、可用同一 run_id 续跑。重新录制后再续跑即可。
    """


class ReplayProvider:
    """按录制回放模型响应，不发起任何外部调用。"""

    def __init__(self, document: RecordingDocument) -> None:
        self.document = document
        self._by_key = {entry.key: entry for entry in document.entries}

    @property
    def model(self) -> str:
        return self.document.model

    def generate(self, system_prompt: str, user_prompt: str, schema: type[T]) -> T:
        key = recording_key(schema.__name__, system_prompt, user_prompt)
        entry = self._by_key.get(key)
        if entry is None:
            raise ReplayMissError(
                f"回放未命中：录制中没有 {schema.__name__} 对应的响应（键 {key}）。"
                "常见原因是输入评论集或 prompt 与录制时不一致；"
                "请运行 scripts/record_demo.py 重新录制。"
            )
        try:
            return schema.model_validate(entry.response)
        except ValidationError as exc:
            raise ReplayMissError(
                f"回放内容与 {schema.__name__} 不匹配（键 {key}）："
                "录制文件可能来自旧版本的 Schema；请重新录制。"
            ) from exc
```

**Task 2 必须自行补入导入。** 按 Task 1 的 R1 裁定，`TypeVar` 与 `RecoverableModelError`
并未被预置（Task 1 只导入了它自己用得到的名字）。请把这两个名字并入文件顶部的导入区，
并在模块级补回 `T = TypeVar("T", bound=BaseModel)`。

这不是格式偏好：上面代码块里的 `class ReplayMissError(RecoverableModelError)` 在**定义期**
求值基类，缺导入会直接 `NameError`，模块根本导入不了。代码块里的 `type[T]` 不受影响
（文件顶部有 `from __future__ import annotations`，注解被推迟求值）。

**本任务已执行的实现补充（计划自身的矛盾，以此为最终形态）。** 上面代码块里的通用未命中消息
不含子串 `Schema`，而 Step 1 的 test 4 断言 `pytest.raises(ReplayMissError, match="Schema")`——
按代码块原样实现，test 4 必然失败；若反过来把 `Schema` 塞进通用消息，test 4 又因为所有未命中
都含该词而失去鉴别力。

最终实现（提交 f9ff613）保留了上述通用消息**逐字不变**（由 test 3 覆盖），并在其之前增加一条
**仅在「同一个 (system, user) 在录制里用过别的 Schema」时触发**的分支，其消息为
`回放未命中：同一请求在录制里用的是 Schema {recorded_names}，本次请求的是 {schema.__name__}；…`。
索引因此从「键 → 条目」扩展为同时记录「(system, user) → 用过的 Schema 名集合」。
test 4 走这条分支，`match="Schema"` 由此获得真实约束力。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_recording.py -q`

Expected: PASS（14 项 —— Task 1 留下 10 项，本任务新增 4 项）

- [ ] **Step 5: 提交**

```bash
git add src/app_review_insights/llm/recording.py tests/test_recording.py
git commit -m "feat: 回放 provider 与未命中失败语义"
```

---


### Task 3: 录制 provider

**Files:**
- Modify: `src/app_review_insights/llm/recording.py`（追加）
- Test: `tests/test_recording.py`（追加）

**Interfaces:**
- Consumes: Task 1 的文档模型与键函数
- Produces:
  - `RecordingProvider(inner, path, *, input_fingerprint: str, model: str)`，带 `.generate(...)` 与 `.entries`
  - `write_recording(document: RecordingDocument, path: Path | str) -> None`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_recording.py（追加）
from typing import Any

from app_review_insights.llm.recording import RecordingProvider, write_recording


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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_recording.py -q`

Expected: FAIL，`ImportError: cannot import name 'RecordingProvider'`

- [ ] **Step 3: 实现 RecordingProvider 与 write_recording**

```python
# 追加到 src/app_review_insights/llm/recording.py
class RecordingProvider:
    """包装真实 provider，把每次成功调用写进录制文件。

    写盘是尽力而为：录制不该影响主流程，失败只记 warning——与 provider 处理用量
    计量失败的方式一致。
    """

    def __init__(
        self,
        inner: Any,
        path: Path | str,
        *,
        input_fingerprint: str,
        model: str,
    ) -> None:
        self.inner = inner
        self.path = Path(path)
        self.input_fingerprint = input_fingerprint
        self.model = model
        self._recorded_at = datetime.now(UTC)
        self.entries: list[RecordingEntry] = []

    def generate(self, system_prompt: str, user_prompt: str, schema: type[T]) -> T:
        result = self.inner.generate(system_prompt, user_prompt, schema)
        self.entries.append(
            RecordingEntry(
                key=recording_key(schema.__name__, system_prompt, user_prompt),
                schema_name=schema.__name__,
                request=RecordingRequest(system=system_prompt, user=user_prompt),
                response=result.model_dump(mode="json"),
            )
        )
        try:
            write_recording(self.document(), self.path)
        except Exception:
            logger.warning("录制写入失败（不影响主流程）：%s", self.path, exc_info=True)
        return result

    def document(self) -> RecordingDocument:
        return RecordingDocument(
            mode=RECORDING_MODE,
            is_live=False,
            recorded_at=self._recorded_at,
            model=self.model,
            input_fingerprint=self.input_fingerprint,
            entries=list(self.entries),
        )


def write_recording(document: RecordingDocument, path: Path | str) -> None:
    """原子写：先落临时文件再替换，避免中途失败留下半个 JSON。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = document.model_dump(mode="json")
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, target)
```

**先给 `src/app_review_insights/storage/cache.py` 追加一行**（该模块已经拥有离线素材，
`load_demo_run` 就在里面）：

```python
#: 仓库自带的样例评论。演示模式与录制脚本共用这一处定义。
#: 放在包内而不是 scripts/，因为界面（ui/main.py）也要用它——
#: 让 src 反过来依赖 scripts 是分层倒置。
SAMPLE_PATH = Path(__file__).resolve().parents[3] / "data" / "samples" / "reviews-sample.json"
```

`Path` 已在 cache.py 顶部导入，无需新增。然后脚本从包里导入它，不再自定义。

**Task 3 必须自行补入导入。** 上面的代码块用到 `datetime.now(UTC)`（`_recorded_at`）与
`os.replace`，而按 R1 裁定这两个名字都没被 Task 1 预置：请把 `import os` 与 `UTC`
（`from datetime import UTC, datetime` 或补进已有的 datetime 导入）并入文件顶部的导入区。
`Path`、`json`、`Any`、`logger` 已由 Task 1 导入，无需重复。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_recording.py -q`

Expected: PASS（20 项）

- [ ] **Step 5: Lint**

Run: `python -m ruff check .` 与 `python -m ruff format --check .`

Expected: 双绿。`chr(0)` / `chr(31)` / `chr(30)` 的写法是为了避开源码里的裸控制字符，保持文件可读。

- [ ] **Step 6: 提交**

```bash
git add src/app_review_insights/llm/recording.py tests/test_recording.py
git commit -m "feat: 录制 provider 与原子写盘"
```

---

### Task 4: 配置项与装配点

**Files:**
- Modify: `src/app_review_insights/config.py`
- Modify: `src/app_review_insights/factory.py:55-89`
- Modify: `.env.example`
- Modify: `README.md`（配置表）
- Test: `tests/test_config.py`、`tests/test_factory.py`

**Interfaces:**
- Consumes: Task 2 的 `ReplayProvider`、Task 3 的 `RecordingProvider`
- Produces:
  - `Settings.demo_mode`（`Literal["auto", "live", "replay"]`，默认 `auto`）
  - `Settings.model_record_path: Path | None`（默认 None，空字符串等同未配置）
  - `Settings.demo_replay_path: Path`（默认 `data/recordings/demo-replay.json`）
  - `Settings.demo_replay_active: bool`
  - `build_pipeline_services(settings, use_fake_provider=False, *, input_fingerprint: str | None = None)`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_config.py（追加）
def test_demo_mode_defaults_to_auto(monkeypatch):
    monkeypatch.delenv("DEMO_MODE", raising=False)
    assert load_settings().demo_mode == "auto"


def test_blank_record_path_becomes_none(monkeypatch):
    monkeypatch.setenv("MODEL_RECORD_PATH", "   ")
    assert load_settings().model_record_path is None


def test_demo_replay_active_follows_mode_and_key(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "auto")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    assert load_settings().demo_replay_active is True

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    assert load_settings().demo_replay_active is False

    monkeypatch.setenv("DEMO_MODE", "replay")
    assert load_settings().demo_replay_active is True
```

```python
# tests/test_factory.py（追加）
from datetime import UTC, datetime

from app_review_insights.errors import InputDataError
from app_review_insights.llm.recording import (
    RECORDING_MODE,
    RecordingDocument,
    write_recording,
)


def _empty_recording(path) -> None:
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


def test_auto_without_key_and_without_recording_yields_no_analyzer(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "auto")
    monkeypatch.setenv("DEMO_REPLAY_PATH", str(tmp_path / "missing.json"))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    assert build_pipeline_services(load_settings()).batch_analyzer is None


def test_replay_mode_without_recording_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "replay")
    monkeypatch.setenv("DEMO_REPLAY_PATH", str(tmp_path / "missing.json"))
    # 断言的是 factory 自己抛的那条消息；load_recording 的「录制文件不存在」
    # 在此路径上不可达（factory 先做了 .exists() 检查）
    with pytest.raises(InputDataError, match="需要录制文件"):
        build_pipeline_services(load_settings())


def test_live_mode_without_key_raises(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "live")
    monkeypatch.setenv("MODEL_ENABLED", "true")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    with pytest.raises(InputDataError, match="需要可用的模型密钥"):
        build_pipeline_services(load_settings())


def test_replay_mode_wires_the_replay_provider(tmp_path, monkeypatch):
    path = tmp_path / "rec.json"
    _empty_recording(path)
    monkeypatch.setenv("DEMO_MODE", "replay")
    monkeypatch.setenv("DEMO_REPLAY_PATH", str(path))

    services = build_pipeline_services(load_settings())

    assert services.batch_analyzer is not None
    assert services.consolidator is not None
    assert services.evidence_auditor is not None


def test_recording_requires_input_fingerprint(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "live")
    monkeypatch.setenv("MODEL_RECORD_PATH", str(tmp_path / "rec.json"))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    with pytest.raises(InputDataError, match="输入指纹"):
        build_pipeline_services(load_settings())
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_config.py tests/test_factory.py -q`

Expected: FAIL，`AttributeError: 'Settings' object has no attribute 'demo_mode'`

- [ ] **Step 3: 加配置项**

```python
# src/app_review_insights/config.py —— 在 Settings 内、模型相关字段附近追加
    demo_mode: Literal["auto", "live", "replay"] = Field(default="auto", alias="DEMO_MODE")
    model_record_path: Path | None = Field(default=None, alias="MODEL_RECORD_PATH")
    demo_replay_path: Path = Field(
        default=Path("data/recordings/demo-replay.json"), alias="DEMO_REPLAY_PATH"
    )

    @field_validator("model_record_path", mode="before")
    @classmethod
    def _blank_path_is_none(cls, value: Any) -> Any:
        """空字符串等同于未配置——.env 里留空是最常见的写法。"""
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value

    @property
    def demo_replay_active(self) -> bool:
        """是否以回放方式运行。

        auto 且无密钥时走回放——这是无密钥部署的默认形态；
        replay 则无视密钥强制回放，供现场演示避免网络与模型波动。
        """
        if self.demo_mode == "replay":
            return True
        return self.demo_mode == "auto" and not self.model_available
```

`Literal` 需要并入 `typing` 导入；`field_validator` 需要并入 `pydantic` 导入（若尚未导入）。

- [ ] **Step 4: 改装配点**

```python
# src/app_review_insights/factory.py —— 用下面两段替换原 build_pipeline_services 的 provider 段
def _build_model_provider(
    settings: Settings,
    repository: RunRepository,
    *,
    input_fingerprint: str | None,
) -> Any | None:
    """按 DEMO_MODE 选择 provider；返回 None 表示当前没有可用的模型。

    回放分支放在最前面：replay 模式即使配了密钥也走回放。
    """
    if settings.demo_replay_active:
        if not settings.demo_replay_path.exists():
            if settings.demo_mode == "replay":
                raise InputDataError(
                    f"DEMO_MODE=replay 需要录制文件，未找到：{settings.demo_replay_path}。"
                    "请先运行 scripts/record_demo.py 生成。"
                )
            # auto 且尚无录制文件：维持既有行为，界面禁用实时分析
            return None
        return ReplayProvider(load_recording(settings.demo_replay_path))

    if not settings.model_available:
        raise InputDataError(
            "DEMO_MODE=live 需要可用的模型密钥，当前未配置。"
            "请设置 DEEPSEEK_API_KEY，或改用 DEMO_MODE=auto/replay。"
        )

    provider: Any = DeepSeekProvider.from_settings(
        settings,
        usage_recorder=repository.record_model_usage,
        budget_check=build_budget_guard(settings, repository),
    )
    if settings.model_record_path is not None:
        if not input_fingerprint:
            raise InputDataError(
                "启用录制（MODEL_RECORD_PATH）时必须提供输入指纹，"
                "否则录制文件无法在回放时校验输入是否一致。"
            )
        provider = RecordingProvider(
            provider,
            settings.model_record_path,
            input_fingerprint=input_fingerprint,
            model=settings.model_name,
        )
    return provider


def build_pipeline_services(
    settings: Settings,
    use_fake_provider: bool = False,
    *,
    input_fingerprint: str | None = None,
) -> PipelineServices:
    repository = RunRepository(settings.database_path)
    common = {
        "repository": repository,
        "collector": AppStoreCollector(),
        "finding_validator": validate_finding_drafts,
        "traceability_validator": validate_traceability,
        "batch_size": settings.batch_review_limit,
        "batch_max_characters": settings.batch_max_characters,
    }
    if use_fake_provider:
        return PipelineServices(batch_analyzer=None, **common)

    provider = _build_model_provider(settings, repository, input_fingerprint=input_fingerprint)
    if provider is None:
        return PipelineServices(batch_analyzer=None, **common)
    return PipelineServices(
        batch_analyzer=lambda reviews, goal: analyze_batch(provider, reviews, goal),
        consolidator=lambda results, goal, reviews: consolidate_findings(
            provider, results, goal, reviews
        ),
        evidence_auditor=lambda findings, reviews, goal: audit_finding_evidence(
            provider, findings, reviews, goal
        ),
        requirement_builder=lambda findings, goal, total: build_requirements(
            provider, findings, goal, total
        ),
        test_case_builder=lambda requirements: generate_test_cases(provider, requirements),
        **common,
    )
```

注意一处语义变化：原来的合并条件 `if use_fake_provider or not settings.model_available` 被拆开了。`use_fake_provider` 仍表示「测试用，彻底不要模型」；`not model_available` 改由 `_build_model_provider` 内部判断——有录制文件时走回放而不是直接没有模型。

新增导入：`from app_review_insights.errors import InputDataError`、`from app_review_insights.llm.recording import ReplayProvider, RecordingProvider, load_recording`。

- [ ] **Step 5: 同步配置文件与文档**

```dotenv
# .env.example —— 追加
# 运行模式：auto=有密钥用真实模型、无密钥回放录制；live=强制真实模型；replay=强制回放
DEMO_MODE=auto
# 录制输出路径（留空 = 不录制；仅供 scripts/record_demo.py 使用）
MODEL_RECORD_PATH=
# 回放读取的录制文件
DEMO_REPLAY_PATH=data/recordings/demo-replay.json
```

README 的配置表追加同三行，取值与含义同上。

- [ ] **Step 6: 运行测试确认通过**

Run: `python -m pytest tests/test_config.py tests/test_factory.py -q`

Expected: PASS

- [ ] **Step 7: 全量回归与 Lint**

Run: `python -m pytest -q`、`python -m ruff check .`、`python -m ruff format --check .`

Expected: 全绿，测试数不少于 459 加新增数

- [ ] **Step 8: 提交**

```bash
git add src/app_review_insights/config.py src/app_review_insights/factory.py .env.example README.md tests/test_config.py tests/test_factory.py
git commit -m "feat: 演示模式配置与按模式装配 provider"
```

---


### Task 5: 录制脚本

**Files:**
- Create: `scripts/record_demo.py`
- Modify: `src/app_review_insights/storage/cache.py`（新增样例路径常量）
- Modify: `tests/test_app_smoke.py`（隔离 DEMO_REPLAY_PATH，见 Step 6）
- Test: `tests/test_record_demo.py`

**Interfaces:**
- Consumes: `build_pipeline_services`、`input_fingerprint`、`AnalysisOrchestrator`、`import_reviews`
- Produces:
  - `app_review_insights.storage.cache.SAMPLE_PATH: Path`（**放在包内，不放在 scripts/**）
  - `DEFAULT_DESTINATION: Path`
  - `record_demo(*, sample_path=SAMPLE_PATH, destination=DEFAULT_DESTINATION, settings=None, services_factory=None) -> RunRecord`
  - `main(argv: list[str] | None = None) -> int`

**先给 `src/app_review_insights/storage/cache.py` 追加一行。** 该模块已经拥有离线素材
（`load_demo_run` 就在里面），样例路径放这里最自然：

```python
#: 仓库自带的样例评论。演示模式与录制脚本共用这一处定义。
#: 放在包内而不是 scripts/：界面（ui/main.py）也要用它，
#: 让 src 反过来依赖 scripts 是分层倒置。
SAMPLE_PATH = Path(__file__).resolve().parents[3] / "data" / "samples" / "reviews-sample.json"
```

`Path` 已在 cache.py 顶部导入，无需新增。脚本从包里导入它，不再自定义。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_record_demo.py
from __future__ import annotations

from pathlib import Path

from app_review_insights.llm.recording import RECORDING_MODE, load_recording
from app_review_insights.storage.cache import SAMPLE_PATH
from scripts.record_demo import record_demo


def test_record_demo_writes_labeled_recording(tmp_path: Path, monkeypatch):
    destination = tmp_path / "demo-replay.json"
    monkeypatch.setenv("MODEL_RECORD_PATH", str(destination))

    record_demo(
        sample_path=SAMPLE_PATH,
        destination=destination,
        services_factory=_recording_services,
    )

    document = load_recording(destination)
    assert document.mode == RECORDING_MODE
    assert document.is_live is False
    assert document.entries, "录制文件必须至少含一次模型调用"
    assert document.input_fingerprint


def test_record_demo_refuses_without_usable_key(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MODEL_ENABLED", "true")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    from scripts.record_demo import main

    assert main([]) == 1
```

```python
# tests/test_record_demo.py —— 替身装配：用预置响应充当模型，全程离线
def _recording_services(settings, *, input_fingerprint):
    """与 factory 的 provider 段同构，只是把最内层换成预置响应的替身。"""
    from app_review_insights.collectors import AppStoreCollector
    from app_review_insights.llm.recording import RecordingProvider
    from app_review_insights.pipeline.analyze import (
        analyze_batch,
        audit_finding_evidence,
        consolidate_findings,
    )
    from app_review_insights.pipeline.orchestrator import PipelineServices
    from app_review_insights.pipeline.planning import build_requirements
    from app_review_insights.pipeline.test_generation import generate_test_cases
    from app_review_insights.pipeline.traceability import validate_traceability
    from app_review_insights.pipeline.validate import validate_finding_drafts
    from app_review_insights.storage import RunRepository
    from tests.conftest import SchemaFakeProvider

    provider = RecordingProvider(
        SchemaFakeProvider(_fake_responses()),
        settings.model_record_path,
        input_fingerprint=input_fingerprint,
        model="stub",
    )
    return PipelineServices(
        repository=RunRepository(settings.database_path),
        collector=AppStoreCollector(),
        finding_validator=validate_finding_drafts,
        traceability_validator=validate_traceability,
        batch_size=settings.batch_review_limit,
        batch_max_characters=settings.batch_max_characters,
        batch_analyzer=lambda reviews, goal: analyze_batch(provider, reviews, goal),
        consolidator=lambda results, goal, reviews: consolidate_findings(
            provider, results, goal, reviews
        ),
        evidence_auditor=lambda findings, reviews, goal: audit_finding_evidence(
            provider, findings, reviews, goal
        ),
        requirement_builder=lambda findings, goal, total: build_requirements(
            provider, findings, goal, total
        ),
        test_case_builder=lambda requirements: generate_test_cases(provider, requirements),
    )
```

`_fake_responses()` 返回与 `tests/conftest.py` 既有夹具同形的响应列表，覆盖依次到达的 Schema
（批次分析、归并、证据审计、需求、用例）。字段以 `llm/schemas.py` 的定义为准；数量按流水线
实际调用次数补足，不足时 `record_demo` 会因替身取空而失败——这本身就是一个有效的红灯。

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_record_demo.py -q`

Expected: FAIL，`ModuleNotFoundError: No module named 'scripts.record_demo'`

- [ ] **Step 3: 实现脚本**

```python
"""用真实模型跑一次样例分析，产出可提交的录制文件。

用法（需要 .env 里有可用的 DEEPSEEK_API_KEY）：

    python scripts/record_demo.py

产出 data/recordings/demo-replay.json，演示模式据此回放整条流水线。
录制只影响写入，不改变任何调用语义。
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app_review_insights.config import Settings, load_settings
from app_review_insights.factory import build_pipeline_services
from app_review_insights.input_parsing import import_reviews
from app_review_insights.llm.recording import input_fingerprint
from app_review_insights.models import AnalysisRequest, RunRecord, SourceType
from app_review_insights.pipeline.orchestrator import AnalysisOrchestrator, PipelineServices
from app_review_insights.storage.cache import SAMPLE_PATH

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DESTINATION = PROJECT_ROOT / "data" / "recordings" / "demo-replay.json"

ANALYSIS_GOAL = "识别影响用户体验与产品增长的核心问题，并形成可追溯需求"


def _recording_settings(settings: Settings, destination: Path) -> Settings:
    """录制必须走真实模型，因此强制 live 并指定输出路径。"""
    return settings.model_copy(update={"demo_mode": "live", "model_record_path": destination})


def record_demo(
    *,
    sample_path: Path = SAMPLE_PATH,
    destination: Path = DEFAULT_DESTINATION,
    settings: Settings | None = None,
    services_factory: Callable[..., PipelineServices] | None = None,
) -> RunRecord:
    """在样例上跑一次分析并把模型调用录下来。"""
    base = settings or load_settings()
    reviews = import_reviews(sample_path.read_bytes(), sample_path.name, app_id="demo")
    recording_settings = _recording_settings(base, destination)
    fingerprint = input_fingerprint(reviews)

    if services_factory is None:
        services = build_pipeline_services(recording_settings, input_fingerprint=fingerprint)
    else:
        services = services_factory(recording_settings, input_fingerprint=fingerprint)

    request = AnalysisRequest(
        source_type=SourceType.JSON,
        analysis_goal=ANALYSIS_GOAL,
        # 样例只有 20 条，而 review_limit 的下限是 100
        review_limit=max(100, len(reviews)),
    )
    return AnalysisOrchestrator(services).start(request, imported_reviews=reviews)


def main(argv: list[str] | None = None) -> int:
    if not load_settings().model_available:
        print("未配置可用的模型密钥，无法录制。请先在 .env 设置 DEEPSEEK_API_KEY。")
        return 1
    run = record_demo()
    print(f"录制完成，run_id={run.run_id}，输出 {DEFAULT_DESTINATION}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_record_demo.py -q`

Expected: PASS

- [ ] **Step 5: 真实录制（人工，一次性，需要密钥与网络）**

Run: `python scripts/record_demo.py`

Expected: 打印 run_id 与输出路径；`data/recordings/demo-replay.json` 生成且 `entries` 非空。
产物提交进仓库，演示模式据此工作。

- [ ] **Step 6: 让全量测试在「录制文件已入库」的前提下仍然全绿**

录制文件一进仓库，**任何断言「无密钥时没有模型」的既有测试都会失效**——因为
`DEMO_MODE=auto` 且默认路径上存在录制时，`build_pipeline_services` 现在返回回放 provider。
Task 4 已实测确认（把合法录制放到默认路径后跑两遍全量）：**恰好一条失败，其余全过**——

`tests/test_app_smoke.py::test_build_services_without_key_keeps_repository_available`
第 48 行 `assert services.batch_analyzer is None`，该用例没有隔离 `DEMO_REPLAY_PATH`。

修法是**隔离**而不是改断言：该用例要验的是「无密钥且无录制时没有模型」，那就把环境收干净。
在它已有的 monkeypatch 段里补一行：

```python
    monkeypatch.setenv("DEMO_REPLAY_PATH", str(tmp_path / "no-recording.json"))
```

不要改成断言回放存在——那会让这条用例的结果取决于磁盘上有没有那个文件，
从确定性测试退化成环境依赖测试。

改完**带着录制文件**再跑一次全量，确认 487 + 本任务新增项全绿：

Run: `python -m pytest -q`，随后再跑一次裸 `pytest.exe -q`（CI 等价）。

- [ ] **Step 7: 提交**

```bash
git add scripts/record_demo.py tests/test_record_demo.py tests/test_app_smoke.py data/recordings/demo-replay.json
git commit -m "feat: 样例分析录制脚本与录制产物"
```

---


### Task 6: 演示模式的输入锁定与界面标注

**Files:**
- Modify: `src/app_review_insights/ui/main.py`（输入表单 `_render_input_form`、模式判定与渲染段、`_prepare_imported_reviews`）
- Modify: `tests/test_app_smoke.py`（隔离 `DEMO_REPLAY_PATH`，见下）
- Test: `tests/test_demo_mode.py`

**Interfaces:**
- Consumes: `Settings.demo_replay_active`、`Settings.demo_replay_path`
- Produces: `_render_input_form(settings, model_ready, *, demo_replay=False)`、`_prepare_imported_reviews(request, upload, *, demo_replay=False)`

**本任务会暴露一批既有测试的环境耦合（执行时已处理，此处记录以免后人重踩）。**
本任务把 `model_ready` 从「只看 `settings.model_available`」改成「或上回放可用」，于是
`tests/test_app_smoke.py` 里三条未隔离 `DEMO_REPLAY_PATH` 的用例立刻失败：
`test_streamlit_page_starts_without_model_key`、
`test_explicit_model_disable_overrides_configured_key`、
`test_waiting_run_keeps_saved_output_visible_without_model_key`
——它们断言按钮禁用，而默认路径上已有录制件，`demo_replay_active and demo_replay_path.exists()`
为真、按钮被启用。**修法是隔离（在那三条的 monkeypatch 段各补
`monkeypatch.setenv("DEMO_REPLAY_PATH", str(tmp_path / "no-recording.json"))`），不是改断言**：
断言改成「禁用或启用都行」会让它们退化成什么都验不了。

本计划里同类「测试依赖环境里碰巧有什么」的缺陷共出现六处（三条配置用例、`test_app_smoke` 一条、
`test_offline_ui` 两处），根因都是 pydantic-settings 在环境变量被删后**回落读 `.env`**，
以及默认路径上存在录制件。**新增 AppTest 用例时，凡涉及模型可用性或按钮状态，都要显式构造前提。**

**本任务执行中暴露的第二处计划缺陷：测试全绿而功能不成立。** 原 Step 3 只改了
`_prepare_imported_reviews` 与输入表单，**没改 `_build_request`**。演示模式下
`source_label="演示样例"`、`upload=None`，`_source_type()` 直接抛
「请选择与导入模式对应的 JSON 或 CSV 评论文件。」——请求根本没构造出来，
点「开始分析」只弹一条输入错误、`run_id` 始终为 None。而原 Step 1 的 4 条测试**全部通过**，
因为 `disabled is False` 只断言按钮不是灰的，**没有断言它点下去真的会发生什么**。

修法：`_build_request(..., *, demo_replay=False)`，演示模式返回
`source_type=SourceType.JSON`、`app_url=None` 的请求（与 `record_demo.py` 的
`AnalysisRequest` 同构）；调用处传 `demo_replay`。

**并补第 5 条测试** `test_demo_mode_runs_the_whole_pipeline_offline`：用**仓库真实录制件**
跑完整条回放，断言无异常、无错误块、`run_id` 存在、`status=completed`、`stage=complete`。
变异验证：把调用处的 `demo_replay` 改回 False → 该用例变红并报出那条输入错误。

> **教训（本计划第四次「断言其实什么都没断言」，也是后果最重的一次）**：
> 前三处影响的是测试强度，这一处差点交付一个**功能完全不成立**的演示模式，
> 而全套测试会替它签字通过。**断言「控件可用」不等于断言「功能可用」**——
> 演示模式这类端到端行为，必须有一条真的把它跑一遍的测试。

**同轮的第三处缺口：分析目标未锁定。** spec 要求「演示模式下必须锁死输入」，
但只想到了数据来源，漏了分析目标——它也进 prompt，访客改一下就会全量回放未命中、
停在 `waiting_for_model`（提示清晰，但访客会以为应用坏了）。

修法与 R2 同构，避免两处事实来源：把那句目标提到包内
（`src/app_review_insights/storage/cache.py` 的 `DEMO_ANALYSIS_GOAL`），
`scripts/record_demo.py` 改为从包里导入（删掉脚本内自己的 `ANALYSIS_GOAL`），
UI 在演示模式下把分析目标输入框禁用并显示该值。
**字符串必须与录制时逐字相同**，否则已有录制件作废、需要重录（花钱）。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_demo_mode.py
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from app_review_insights.llm.recording import (
    RECORDING_MODE,
    RecordingDocument,
    write_recording,
)
from streamlit.testing.v1 import AppTest


def _recording_at(path: Path) -> Path:
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


def _replay_app(tmp_path, monkeypatch) -> AppTest:
    monkeypatch.chdir(tmp_path)
    recording = _recording_at(tmp_path / "demo-replay.json")
    monkeypatch.setenv("DEMO_MODE", "replay")
    monkeypatch.setenv("DEMO_REPLAY_PATH", str(recording))
    return AppTest.from_file("app.py").run()


def test_demo_mode_shows_a_labeled_banner(tmp_path, monkeypatch):
    app = _replay_app(tmp_path, monkeypatch)
    messages = [block.value for block in app.warning]
    assert any("演示模式" in message for message in messages)
    assert any("不调用外部 API" in message for message in messages)


def test_demo_mode_locks_the_input_source(tmp_path, monkeypatch):
    app = _replay_app(tmp_path, monkeypatch)
    source = next(box for box in app.selectbox if box.label == "数据来源")
    assert source.disabled is True
    assert source.value == "演示样例"
```

再加一条断言「开始分析」可点——这是演示模式的关键行为。若该按钮当前未设置 `key`，
先补 `key="start-analysis"`：

```python
def test_demo_mode_enables_the_start_button(tmp_path, monkeypatch):
    app = _replay_app(tmp_path, monkeypatch)
    assert app.button(key="start-analysis").disabled is False
```

两种演示必须能区分：既有的「查看历史缓存演示」开关是只读浏览历史结果，本节的演示模式是
重跑整条流水线。Step 4 的回归里同时打开两者，确认各自的标注互不混淆。URL 参数的断言在 Task 7。

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_demo_mode.py -q`

Expected: FAIL，找不到「演示模式」提示

- [ ] **Step 3: 改模式判定与标注**

```python
# src/app_review_insights/ui/main.py —— 替换原来的 model_ready = settings.model_available
    # 密钥已填写（无论有效无效）即可开始：无效密钥会在模型环节失败并保留检查点。
    # 演示模式（回放）同样可以开始——这是无密钥部署的默认形态。
    demo_replay = settings.demo_replay_active and settings.demo_replay_path.exists()
    model_ready = settings.model_available or demo_replay
```

```python
# 在 render_model_status(...) 调用之后、_render_input_form(...) 之前插入
    if demo_replay:
        st.warning(
            "演示模式：回放一次真实运行的模型输出，不调用外部 API，"
            "输入固定为仓库自带样例。",
            icon=":material/replay:",
        )
```

```python
# _render_input_form 增加 demo_replay 形参，并把数据来源控件分支
def _render_input_form(settings: Settings, model_ready: bool, *, demo_replay: bool = False):
    ...
    if demo_replay:
        st.selectbox(
            "数据来源",
            ["演示样例"],
            disabled=True,
            help="演示模式回放的是样例上的运行结果；换成别的输入将无法命中录制。",
            key="source-mode-locked",
        )
        st.caption(f"样例文件：{SAMPLE_PATH.name}")
    else:
        ...  # 原有三选一与各模式控件保持不变
```

```python
# _prepare_imported_reviews 增加分支：演示模式直接读样例，忽略上传与 URL
def _prepare_imported_reviews(request: AnalysisRequest, upload, *, demo_replay: bool = False):
    if demo_replay:
        return import_reviews(SAMPLE_PATH.read_bytes(), SAMPLE_PATH.name, app_id="demo")
    if request.source_type == SourceType.ONLINE:
        return None
    ...
```

调用处同步改为 `_render_input_form(settings, model_ready, demo_replay=demo_replay)` 与
`_prepare_imported_reviews(request, upload, demo_replay=demo_replay)`。`SAMPLE_PATH` 从
**`app_review_insights.storage.cache`** 导入（Task 5 已把它放进包内）——**不要**从
`scripts.record_demo` 导入：那会让 src 依赖 scripts，是分层倒置。

- [ ] **Step 4: 让界面在装配失败时降级，而不是整页报错**

Task 4 的复核发现（Important，plan-mandated）：`factory._build_model_provider` 在
`DEMO_MODE=live` 且无可用密钥时抛 `InputDataError`（这是刻意的——显式选了 live 就该
响亮地失败，而不是悄悄回放）。但 `ui/main.py:464` 是在**页面渲染路径**上无保护地调
`build_services()` 的，于是该配置下：

- 改动前：页面正常渲染，按钮禁用并说明原因；
- 改动后：整页抛异常。

这违反两条既有承诺——`AGENTS.md` 的「没有密钥也能启动……按钮保持禁用并说明原因」，
以及设计约束 #3「降级不崩溃，不能把异常抛到 UI」。

**裁定**：`factory` 保留抛出（live 的语义就是别静默降级，Task 4 的测试不动）；
**界面**负责兜底降级。`monitor/worker.py:131` **不兜底**——常驻服务在显式配置错误下
拒绝启动是对的（fail fast），只有交互式页面才该渲染出来解释原因。这个不对称是刻意的。

**Step 4a：写失败测试**

```python
# tests/test_demo_mode.py（追加）
def test_live_mode_without_key_renders_instead_of_crashing(tmp_path, monkeypatch):
    """显式选了 live 却没密钥：页面要能渲染并说明原因，不能整页报错。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEMO_MODE", "live")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "runs.sqlite3"))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)

    app = AppTest.from_file("app.py").run()

    assert not app.exception, f"页面不应抛异常，实际：{app.exception}"
    messages = [block.value for block in app.warning] + [block.value for block in app.error]
    assert any("密钥" in message for message in messages)
```

**Step 4b：确认失败** —— Run: `python -m pytest tests/test_demo_mode.py::test_live_mode_without_key_renders_instead_of_crashing -q`
Expected: FAIL（页面抛 `InputDataError`）

**Step 4c：实现兜底**

```python
# src/app_review_insights/ui/main.py —— 替换 main() 里的 services = build_services()
    settings = load_settings()
    services, wiring_error = _build_services_or_degrade()
```

```python
# 新增：装配失败时降级成「无模型」的流水线，把原因交给页面显示
def _build_services_or_degrade() -> tuple[PipelineServices, str | None]:
    """装配失败不抛到 UI：退化成无模型的流水线，由页面说明原因。

    配置错误（例如 DEMO_MODE=live 却没密钥）是用户能在页面上看懂并修好的，
    不该以整页 traceback 的形式呈现——这是设计约束「降级不崩溃」的直接落实。
    """
    try:
        return build_services(), None
    except AppReviewInsightsError as exc:
        fallback = _build_pipeline_services(load_settings(), use_fake_provider=True)
        return fallback, str(exc)
```

在 `st.title(...)` 之前渲染这段原因（复用既有警告样式）：

```python
    if wiring_error:
        st.warning(f"模型装配未完成：{wiring_error}", icon=":material/key_off:")
```

`AppReviewInsightsError` 已由 `errors.py` 提供，是 `InputDataError` 的基类，
**不要**为它新增异常类。

**Step 4d：确认通过** —— Run: `python -m pytest tests/test_demo_mode.py tests/test_app_smoke.py tests/test_offline_ui.py -q`
Expected: PASS

- [ ] **Step 5: 运行测试确认通过**

Run: `python -m pytest tests/test_demo_mode.py tests/test_app_smoke.py tests/test_offline_ui.py -q`

Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add src/app_review_insights/ui/main.py tests/test_demo_mode.py
git commit -m "feat: 演示模式输入锁定与界面标注"
```

---

### Task 7: 输入状态的 URL 参数

**Files:**
- Modify: `src/app_review_insights/ui/main.py`（`_sync_inputs_to_query_params` 及其调用处）
- Test: `tests/test_url_params.py`

**Interfaces:**
- Consumes: Task 6 的 `demo_replay` 布尔量
- Produces: `_sync_inputs_to_query_params(..., *, demo_replay: bool = False)`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_url_params.py
from __future__ import annotations

from streamlit.testing.v1 import AppTest

from tests.test_demo_mode import _recording_at


def test_demo_mode_writes_no_query_params(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    recording = _recording_at(tmp_path / "demo-replay.json")
    monkeypatch.setenv("DEMO_MODE", "replay")
    monkeypatch.setenv("DEMO_REPLAY_PATH", str(recording))

    app = AppTest.from_file("app.py").run()

    assert dict(app.query_params) == {}


def test_empty_values_are_not_written(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEMO_MODE", raising=False)

    app = AppTest.from_file("app.py").run()

    assert "url" not in app.query_params
    assert "upload" not in app.query_params
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_url_params.py -q`

Expected: FAIL，`url` 或 `upload` 出现在 `query_params` 中

- [ ] **Step 3: 改写入策略**

```python
# src/app_review_insights/ui/main.py —— 替换 _sync_inputs_to_query_params
def _sync_inputs_to_query_params(
    source_label: str,
    app_url: str,
    goal: str,
    review_limit: int,
    upload,
    *,
    demo_replay: bool = False,
) -> None:
    """把当前输入写进 URL 查询参数，用于浏览器刷新后恢复。

    两种情况下不写：
    - 演示模式：输入被锁定为样例，恢复机制没有意义，而分享链接会连带把填写者
      当次的输入带出去；
    - 空值：空参数只让链接变长，url= 这类空值尤其容易被误读为「有一个地址」。
    """
    if demo_replay:
        return
    params = st.query_params
    params["mode"] = source_label
    if source_label == "在线采集" and app_url:
        params["url"] = app_url
    if goal:
        params["goal"] = goal
    params["limit"] = str(review_limit)
    restored = st.session_state.get("restored-upload")
    upload_name = Path(restored).name if restored else None
    if not upload_name and upload is not None and upload.name:
        upload_name = upload.name
    if upload_name:
        params["upload"] = upload_name
```

调用处改为 `_sync_inputs_to_query_params(source_label, app_url, analysis_goal, review_limit, upload, demo_replay=demo_replay)`。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_url_params.py -q`

Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/app_review_insights/ui/main.py tests/test_url_params.py
git commit -m "fix: 演示模式不写 URL 参数，空值不再落入分享链接"
```

---


### Task 8: 文档修订

**Files:**
- Modify: `docs/deploy-streamlit-cloud.md`
- Modify: `README.md`
- Modify: `CHANGELOG.md`

**Interfaces:** 无代码接口。

- [ ] **Step 1: 修订预算上限的表述**

`docs/deploy-streamlit-cloud.md` 中「公开 demo 一定要同时设预算上限」一段给的是虚假安全感，改写为：

- `MODEL_BUDGET_USD_PER_RUN` **完全有效**，单次运行不会超；
- `MODEL_BUDGET_USD_PER_DAY` **不是硬上限**：它由 `llm/budget.py` 的
  `spent_usd(None, start_of_today_utc())` 从 `model_usage` 表读取，而该表位于
  `data/runs/runs.sqlite3`；平台的文件系统在重启、休眠唤醒与重新部署后重置，
  当日计数随之归零；
- 因此公开部署**不应依赖**该上限兜底：正确做法是不挂密钥、以演示模式回放。

- [ ] **Step 2: 补演示模式说明**

同一文档补：演示模式的触发条件、界面标注的含义、录制文件的生成与更新方式
（`python scripts/record_demo.py`）、以及「分享请使用不带查询参数的裸链接」。

- [ ] **Step 3: 更新 README 与 CHANGELOG**

README：功能一览补「离线演示模式」一条；配置表补 `DEMO_MODE` / `MODEL_RECORD_PATH` /
`DEMO_REPLAY_PATH`（Task 4 已加，此处核对是否遗漏）。
CHANGELOG：新增一条，写清演示模式、模型层录制回放与 URL 参数修复。

- [ ] **Step 4: 全量验证**

Run: `python -m pytest -q`、`python -m ruff check .`、`python -m ruff format --check .`

Expected: 全绿

- [ ] **Step 5: 提交**

```bash
git add docs/deploy-streamlit-cloud.md README.md CHANGELOG.md
git commit -m "docs: 演示模式说明与预算上限表述修订"
```

---

## 完成标准

1. 无密钥启动进入演示模式：输入锁定为自带样例、顶部有明确标注、「开始分析」可点；
2. 点「开始分析」跑完十一个阶段并给出结果，全程无外部 API 调用；
3. 同一次演示重复运行结果一致；
4. 篡改录制文件的 `mode` 或 `is_live` 会被拒绝加载；键未命中抛 `ReplayMissError`
   并停在检查点，状态为 `waiting_for_model`；
5. 有密钥且 `DEMO_MODE=auto` 时，行为与改动前一致；
6. `ruff check .` 与 `ruff format --check .` 双绿，测试数不少于 459 加本次新增；
7. 演示模式下 URL 不含任何查询参数；非演示模式下空值不入 URL。

## 不在本次范围

- 访问控制：公开演示不挂密钥，没有需要保护的成本面；登录墙会让访客无法进入。属独立子项目。
- 后台执行与协作式取消：回放无网络调用、秒级完成，不存在阻塞会话的问题。属独立子项目。
- 多用户隔离、外部任务队列、PostgreSQL 迁移。

