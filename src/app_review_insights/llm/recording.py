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

#: 录制文件的模式标记。与 storage/cache.py 的 historical_cache_demo 同一套约定：
#: 离线产物必须显式标注，且永远不是实时结果。
RECORDING_MODE = "recorded_live_run"

#: 请求三段之间的分隔符，避免「前一段结尾拼上后一段开头」撞出同一个键。
_FIELD_SEP = chr(0)

#: 录制与回放共用的返回类型：按请求的 Schema 反序列化。
T = TypeVar("T", bound=BaseModel)


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
    """读取并校验录制文件；读不出来或标注不正确都拒绝加载。

    读不出来一律是 InputDataError：文件不存在、路径指向目录、无读权限、不是
    UTF-8 文本（Windows 记事本存成「Unicode」就是这种文件），都转成同一种域内
    错误，调用方（UI / 流水线装配）拿到的是中文消息而不是英文 traceback。

    抛 InputDataError 而不是 RecoverableModelError：文件缺失或标错属于配置与素材
    问题，应当在运行开始前暴露，重试没有意义。
    """
    target = Path(path)
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise InputDataError(f"录制文件不存在：{target}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        # UnicodeDecodeError 继承自 ValueError 而非 OSError，两者必须都列出来
        raise InputDataError(f"录制文件无法读取：{target}（{exc}）") from exc
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
        # 同一个 (system, user) 可能被不同 Schema 各录一次，所以记集合而不是单条
        self._schemas_by_request: dict[tuple[str, str], set[str]] = {}
        for entry in document.entries:
            request = (entry.request.system, entry.request.user)
            self._schemas_by_request.setdefault(request, set()).add(entry.schema_name)

    @property
    def model(self) -> str:
        return self.document.model

    def generate(self, system_prompt: str, user_prompt: str, schema: type[T]) -> T:
        """按键取回录制响应并按请求的 Schema 反序列化，未命中抛 ReplayMissError。"""
        key = recording_key(schema.__name__, system_prompt, user_prompt)
        entry = self._by_key.get(key)
        if entry is None:
            recorded = self._schemas_by_request.get((system_prompt, user_prompt))
            if recorded and schema.__name__ not in recorded:
                recorded_names = "、".join(sorted(recorded))
                raise ReplayMissError(
                    f"回放未命中：同一请求在录制里用的是 Schema {recorded_names}，"
                    f"本次请求的是 {schema.__name__}；请确认调用方与录制时一致。"
                )
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
