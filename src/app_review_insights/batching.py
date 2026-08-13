from app_review_insights.models import Review


def make_review_batches(
    reviews: list[Review], max_reviews: int, max_characters: int
) -> list[list[Review]]:
    if max_reviews <= 0 or max_characters <= 0:
        raise ValueError("batch limits must be positive")

    batches: list[list[Review]] = []
    current: list[Review] = []
    current_characters = 0

    for review in reviews:
        review_characters = len(review.content_original)
        would_overflow = current and (
            len(current) >= max_reviews
            or current_characters + review_characters > max_characters
        )
        if would_overflow:
            batches.append(current)
            current = []
            current_characters = 0

        if review_characters > max_characters and not current:
            batches.append([review])
            continue

        current.append(review)
        current_characters += review_characters

    if current:
        batches.append(current)
    return batches
