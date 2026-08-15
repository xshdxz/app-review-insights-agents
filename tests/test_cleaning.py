from datetime import UTC, datetime

from app_review_insights.cleaning import clean_reviews
from app_review_insights.models import Review


def review(review_id: str, content: str, rating: int = 1) -> Review:
    return Review(
        review_id=review_id,
        app_id="app-1",
        content_original=content,
        rating=rating,
        published_at=datetime.now(UTC),
        source="fixture",
    )


def test_clean_reviews_removes_exact_and_near_duplicates():
    result = clean_reviews(
        [
            review("r-1", "  Timer   freezes after pause. "),
            review("r-2", "Timer freezes after pause."),
            review("r-3", "Timer freezes after pausing!"),
            review("r-4", "Subscription price is unclear.", rating=2),
        ],
        near_duplicate_threshold=94,
    )

    assert [item.review_id for item in result.reviews] == ["r-1", "r-4"]
    assert result.stats.input_count == 4
    assert result.stats.output_count == 2
    assert result.stats.exact_duplicates == 1
    assert result.stats.near_duplicates == 1
    assert all(item.content_hash for item in result.reviews)


def test_clean_reviews_keeps_conflicting_duplicate_ids_traceable():
    result = clean_reviews(
        [
            review("same-id", "The timer freezes after pause."),
            review("same-id", "The timer freezes after pause."),
            review("same-id", "The subscription price is unclear."),
        ]
    )

    ids = [item.review_id for item in result.reviews]
    assert ids[0] == "same-id"
    assert ids[1].startswith("same-id--")
    assert len(ids) == len(set(ids)) == 2
    assert result.stats.exact_duplicates == 1
    assert result.stats.review_id_collisions == 1


def test_clean_reviews_removes_whitespace_only_content():
    result = clean_reviews([review("r-empty", "   \n\t  ")])

    assert result.reviews == []
    assert result.stats.empty_removed == 1


def test_clean_reviews_detects_language_but_preserves_existing_value():
    detected = review("r-en", "The subscription renewal date is difficult to understand.")
    preserved = review("r-manual", "This language value is supplied by the importer.")
    preserved.language = "manual"

    result = clean_reviews([detected, preserved])

    assert result.reviews[0].language == "en"
    assert result.reviews[1].language == "manual"


def test_near_duplicates_with_different_ratings_are_kept():
    result = clean_reviews(
        [
            review("r-1", "The subscription price is unclear.", rating=1),
            review("r-2", "The subscription price is unclear!", rating=5),
        ],
        near_duplicate_threshold=90,
    )

    assert [item.review_id for item in result.reviews] == ["r-1", "r-2"]


def test_partial_phrase_does_not_remove_a_substantially_longer_review():
    result = clean_reviews(
        [
            review("r-short", "Timer freezes after pause."),
            review(
                "r-long",
                "Timer freezes after pause, and the subscription renewal details are also unclear.",
            ),
        ],
        near_duplicate_threshold=94,
    )

    assert [item.review_id for item in result.reviews] == ["r-short", "r-long"]


def test_punctuation_only_reviews_do_not_crash_near_duplicate_comparison():
    result = clean_reviews(
        [
            review("r-exclamation", "!!!"),
            review("r-question", "???"),
        ]
    )

    assert [item.review_id for item in result.reviews] == [
        "r-exclamation",
        "r-question",
    ]
