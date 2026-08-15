import hashlib
import re

from langdetect import DetectorFactory, LangDetectException, detect
from pydantic import BaseModel, Field
from rapidfuzz.fuzz import partial_ratio

from app_review_insights.models import Review

DetectorFactory.seed = 0


class CleaningStats(BaseModel):
    input_count: int = Field(ge=0)
    output_count: int = Field(ge=0)
    exact_duplicates: int = Field(ge=0)
    near_duplicates: int = Field(ge=0)
    empty_removed: int = Field(ge=0)


class CleaningResult(BaseModel):
    reviews: list[Review]
    stats: CleaningStats


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _comparison_text(value: str) -> str:
    return re.sub(r"[^\w\s]", "", value.casefold())


def _detect_language(value: str) -> str | None:
    if len(value) < 12:
        return None
    try:
        return detect(value)
    except LangDetectException:
        return None


def _is_near_duplicate(
    previous: Review,
    incoming_rating: int,
    comparison_text: str,
    threshold: int,
) -> bool:
    if previous.rating != incoming_rating:
        return False

    previous_text = _comparison_text(previous.content_original)
    longest_length = max(len(previous_text), len(comparison_text))
    if longest_length == 0:
        return False

    length_ratio = min(len(previous_text), len(comparison_text)) / longest_length
    return length_ratio >= 0.8 and partial_ratio(previous_text, comparison_text) >= threshold


def clean_reviews(reviews: list[Review], near_duplicate_threshold: int = 96) -> CleaningResult:
    kept: list[Review] = []
    hashes: set[str] = set()
    exact_duplicates = 0
    near_duplicates = 0
    empty_removed = 0

    for incoming in reviews:
        normalized = normalize_text(incoming.content_original)
        if not normalized:
            empty_removed += 1
            continue

        normalized_folded = normalized.casefold()
        comparison_text = _comparison_text(normalized)
        content_hash = hashlib.sha256(
            f"{incoming.rating}|{normalized_folded}".encode()
        ).hexdigest()[:20]

        if content_hash in hashes:
            exact_duplicates += 1
            continue

        if any(
            _is_near_duplicate(
                previous,
                incoming.rating,
                comparison_text,
                near_duplicate_threshold,
            )
            for previous in kept
        ):
            near_duplicates += 1
            continue

        hashes.add(content_hash)
        kept.append(
            incoming.model_copy(
                update={
                    "content_original": normalized,
                    "content_hash": content_hash,
                    "language": incoming.language or _detect_language(normalized),
                }
            )
        )

    return CleaningResult(
        reviews=kept,
        stats=CleaningStats(
            input_count=len(reviews),
            output_count=len(kept),
            exact_duplicates=exact_duplicates,
            near_duplicates=near_duplicates,
            empty_removed=empty_removed,
        ),
    )
