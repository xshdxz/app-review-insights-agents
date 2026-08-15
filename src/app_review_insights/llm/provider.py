import json
import time
from collections.abc import Sequence
from typing import Any, TypeVar

from openai import OpenAI
from pydantic import BaseModel, ValidationError

from app_review_insights.errors import RecoverableModelError

T = TypeVar("T", bound=BaseModel)


class DeepSeekProvider:
    def __init__(
        self,
        client: Any,
        model: str,
        max_retries: int = 2,
        retry_delays: Sequence[float] | None = None,
        max_tokens: int = 8192,
    ):
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if max_tokens <= 0:
            raise ValueError("max_tokens must be positive")

        delays = tuple((1, 2) if retry_delays is None else retry_delays)
        if any(delay < 0 for delay in delays):
            raise ValueError("retry_delays must be non-negative")

        self.client = client
        self.model = model
        self.max_retries = max_retries
        self.retry_delays = delays
        self.max_tokens = max_tokens

    @classmethod
    def from_settings(cls, settings):
        return cls(
            client=OpenAI(
                api_key=settings.deepseek_api_key,
                base_url=settings.model_base_url,
                timeout=settings.model_timeout_seconds,
                max_retries=0,
            ),
            model=settings.model_name,
            max_retries=settings.model_max_retries,
            max_tokens=settings.model_max_tokens,
        )

    def generate(self, system_prompt: str, user_prompt: str, schema: type[T]) -> T:
        last_error: Exception | None = None
        schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        messages = [
            {
                "role": "system",
                "content": (
                    f"{system_prompt}\n\n"
                    "必须只输出一个合法 JSON 对象，并严格满足以下 JSON Schema。"
                    "数组字段即使只有一项也必须使用数组，不能使用字符串或 null。\n"
                    f"JSON Schema：{schema_json}"
                ),
            },
            {"role": "user", "content": user_prompt},
        ]
        for attempt in range(self.max_retries + 1):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    temperature=0.1,
                    max_tokens=self.max_tokens,
                    response_format={"type": "json_object"},
                    messages=messages,
                )
                content = response.choices[0].message.content or "{}"
                return schema.model_validate_json(content)
            except ValidationError as exc:
                last_error = exc
                if attempt < self.max_retries:
                    messages = [
                        *messages,
                        {"role": "assistant", "content": content},
                        {
                            "role": "user",
                            "content": (
                                "上一次 JSON 输出未通过 Schema 校验。"
                                "请根据以下错误重新生成完整 JSON 对象，不要解释：\n"
                                f"{exc}"
                            ),
                        },
                    ]
            except Exception as exc:
                last_error = exc

            if attempt < self.max_retries and self.retry_delays:
                delay_index = min(attempt, len(self.retry_delays) - 1)
                if self.retry_delays[delay_index] > 0:
                    time.sleep(self.retry_delays[delay_index])

        raise RecoverableModelError(f"模型调用失败，可从检查点继续：{last_error}") from last_error
