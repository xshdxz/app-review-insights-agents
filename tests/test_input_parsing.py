import json

import pytest

from app_review_insights.errors import InputDataError
from app_review_insights.input_parsing import import_reviews, parse_app_store_url


def test_parse_us_app_store_url():
    parsed = parse_app_store_url(
        "https://apps.apple.com/us/app/workout-for-women-home-gym/id839285684"
    )

    assert parsed.app_id == "839285684"
    assert parsed.country == "us"
    assert parsed.slug == "workout-for-women-home-gym"


def test_parse_app_store_url_rejects_non_us_storefront():
    with pytest.raises(InputDataError, match="美国区"):
        parse_app_store_url("https://apps.apple.com/cn/app/example/id123456789")


def test_import_json_normalizes_aliases():
    payload = json.dumps(
        [
            {
                "id": "r-1",
                "content": "The trial price is unclear.",
                "rating": 2,
                "date": "2026-08-01T10:00:00Z",
                "version": "8.5.0",
            }
        ]
    ).encode()

    reviews = import_reviews(payload, "reviews.json", app_id="imported-app")

    assert reviews[0].review_id == "r-1"
    assert reviews[0].content_original == "The trial price is unclear."
    assert reviews[0].app_version == "8.5.0"
    assert reviews[0].source == "import:json"


def test_import_csv_accepts_documented_columns():
    payload = (
        b"review_id,content,rating,published_at,title\n"
        b"r-2,Workout timer freezes,1,2026-08-02T10:00:00Z,Timer bug\n"
    )

    reviews = import_reviews(payload, "reviews.csv", app_id="imported-app")

    assert reviews[0].title == "Timer bug"
    assert reviews[0].source == "import:csv"


def test_import_rejects_unsupported_file_type():
    with pytest.raises(InputDataError, match="仅支持"):
        import_reviews(b"content", "reviews.txt", app_id="imported-app")


def test_import_reports_missing_content_with_row_number():
    payload = json.dumps(
        [{"id": "r-1", "rating": 2, "date": "2026-08-01T10:00:00Z"}]
    ).encode()

    with pytest.raises(InputDataError, match="第 1 条评论缺少 content"):
        import_reviews(payload, "reviews.json", app_id="imported-app")
