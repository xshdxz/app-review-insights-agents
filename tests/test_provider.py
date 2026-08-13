from datetime import UTC, datetime

import pytest

from app_review_insights.batching import make_review_batches
from app_review_insights.errors import RecoverableModelError
from app_review_insights.llm.provider import DeepSeekProvider
from app_review_insights.llm.schemas import BatchAnalysisResult, RequirementPlanResult
from app_review_insights.models import Review


def review(review_id: str, content: str) -> Review:
    return Review(
        review_id=review_id,
        app_id="app-1",
        content_original=content,
        rating=1,
        published_at=datetime.now(UTC),
        source="fixture",
    )


def test_batching_respects_count_and_character_limits():
    reviews = [review(f"r-{index}", "x" * 700) for index in range(5)]

    batches = make_review_batches(reviews, max_reviews=100, max_characters=1500)

    assert [len(batch) for batch in batches] == [2, 2, 1]


def test_batching_keeps_an_oversized_review_in_its_own_batch():
    batches = make_review_batches(
        [review("large", "x" * 2000), review("small", "y" * 10)],
        max_reviews=100,
        max_characters=1500,
    )

    assert [[item.review_id for item in batch] for batch in batches] == [
        ["large"],
        ["small"],
    ]


@pytest.mark.parametrize(
    ("max_reviews", "max_characters"),
    [(0, 100), (10, 0)],
)
def test_batching_rejects_non_positive_limits(max_reviews, max_characters):
    with pytest.raises(ValueError, match="positive"):
        make_review_batches(
            [review("r-1", "text")],
            max_reviews=max_reviews,
            max_characters=max_characters,
        )


class FakeCompletions:
    def __init__(self, contents):
        self.contents = list(contents)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = self.contents.pop(0)
        message = type("Message", (), {"content": content})
        choice = type("Choice", (), {"message": message})
        return type("Response", (), {"choices": [choice]})


def fake_client(contents):
    completions = FakeCompletions(contents)
    client = type(
        "Client",
        (),
        {"chat": type("Chat", (), {"completions": completions})()},
    )()
    return client, completions


def test_provider_validates_json_against_schema():
    client, completions = fake_client(
        ['{"findings": [], "batch_limitations": []}']
    )
    provider = DeepSeekProvider(
        client=client,
        model="deepseek-chat",
        max_retries=0,
        retry_delays=(),
    )

    result = provider.generate("system", "user", BatchAnalysisResult)

    assert result.findings == []
    assert completions.calls[0]["response_format"] == {"type": "json_object"}
    assert completions.calls[0]["temperature"] == 0.1


def test_provider_preserves_explicit_empty_retry_delays():
    client, _ = fake_client([])

    provider = DeepSeekProvider(
        client=client,
        model="deepseek-chat",
        retry_delays=(),
    )

    assert provider.retry_delays == ()


def test_provider_rejects_negative_retry_count():
    client, _ = fake_client([])

    with pytest.raises(ValueError, match="non-negative"):
        DeepSeekProvider(
            client=client,
            model="deepseek-chat",
            max_retries=-1,
        )


def test_provider_rejects_negative_retry_delays():
    client, _ = fake_client([])

    with pytest.raises(ValueError, match="non-negative"):
        DeepSeekProvider(
            client=client,
            model="deepseek-chat",
            retry_delays=(-1,),
        )


def test_requirement_plan_requires_five_to_ten_requirements():
    with pytest.raises(ValueError):
        RequirementPlanResult.model_validate({"requirements": []})


def test_provider_retries_invalid_json_then_succeeds():
    client, completions = fake_client(
        ["not-json", '{"findings": [], "batch_limitations": ["retried"]}']
    )
    provider = DeepSeekProvider(
        client=client,
        model="deepseek-chat",
        max_retries=1,
        retry_delays=(0,),
    )

    result = provider.generate("system", "user", BatchAnalysisResult)

    assert result.batch_limitations == ["retried"]
    assert len(completions.calls) == 2


def test_provider_raises_recoverable_error_after_bounded_retries():
    client, completions = fake_client(["not-json", "still-not-json"])
    provider = DeepSeekProvider(
        client=client,
        model="deepseek-chat",
        max_retries=1,
        retry_delays=(0,),
    )

    with pytest.raises(RecoverableModelError, match="检查点继续"):
        provider.generate("system", "user", BatchAnalysisResult)

    assert len(completions.calls) == 2
