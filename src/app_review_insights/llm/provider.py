import time
from collections.abc import Sequence
from typing import Any, TypeVar

from openai import OpenAI
from pydantic import BaseModel

from app_review_insights.errors import RecoverableModelError

T = TypeVar("T", bound=BaseModel)


class DeepSeekProvider:
    def __init__(
        self,
        client: Any,
        model: str,
        max_retries: int = 2,
        retry_delays: Sequence[float] | None = None,
    ):
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")

        delays = tuple((1, 2) if retry_delays is None else retry_delays)
        if any(delay < 0 for delay in delays):
            raise ValueError("retry_delays must be non-negative")

        self.client = client
        self.model = model
        self.max_retries = max_retries
        self.retry_delays = delays

    @classmethod
    def from_settings(cls, settings):
        return cls(
            client=OpenAI(
                api_key=settings.deepseek_api_key,
                base_url=settings.model_base_url,
                timeout=settings.model_timeout_seconds,
            ),
            model=settings.model_name,
            max_retries=settings.model_max_retries,
        )

    def generate(self, system_prompt: str, user_prompt: str, schema: type[T]) -> T:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    temperature=0.1,
                    response_format={"type": "json_object"},
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                )
                content = response.choices[0].message.content or "{}"
                return schema.model_validate_json(content)
            except Exception as exc:
                last_error = exc
                if attempt < self.max_retries and self.retry_delays:
                    delay_index = min(attempt, len(self.retry_delays) - 1)
                    if self.retry_delays[delay_index] > 0:
                        time.sleep(self.retry_delays[delay_index])

        raise RecoverableModelError(
            f"模型调用失败，可从检查点继续：{last_error}"
        ) from last_error
