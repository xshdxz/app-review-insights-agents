from datetime import UTC, datetime
from math import ceil
from typing import Any

import httpx

from app_review_insights.errors import CollectionError
from app_review_insights.input_parsing import parse_app_store_url
from app_review_insights.models import Review

_RSS_URL = (
    "https://itunes.apple.com/us/rss/customerreviews/page={page}/"
    "id={app_id}/sortby=mostrecent/json"
)
_REVIEWS_PER_PAGE = 50
_MAX_PAGES = 10


class AppStoreCollector:
    def __init__(self, client: Any | None = None, timeout_seconds: float = 20):
        self.client = client or httpx.Client(
            timeout=timeout_seconds,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/139 Safari/537.36"
                )
            },
            follow_redirects=True,
        )

    def collect(self, app_url: str, limit: int) -> list[Review]:
        parsed = parse_app_store_url(app_url)
        maximum_limit = _REVIEWS_PER_PAGE * _MAX_PAGES
        if limit > maximum_limit:
            raise CollectionError(
                "Apple RSS 在线采集最多 500 条评论，请将数量调至 500 以内，"
                "或改用 JSON/CSV 导入更多评论"
            )

        bounded_limit = max(1, limit)
        page_count = ceil(bounded_limit / _REVIEWS_PER_PAGE)
        reviews: list[Review] = []
        seen_ids: set[str] = set()

        for page in range(1, page_count + 1):
            try:
                response = self.client.get(
                    _RSS_URL.format(page=page, app_id=parsed.app_id)
                )
                response.raise_for_status()
                entries = response.json().get("feed", {}).get("entry", []) or []
            except Exception as exc:
                if reviews:
                    return reviews
                raise CollectionError(
                    f"美国区评论采集失败，请稍后重试或改用 JSON/CSV 导入：{exc}"
                ) from exc

            page_reviews = [entry for entry in entries if "im:rating" in entry]
            if not page_reviews:
                break

            for index, item in enumerate(page_reviews):
                try:
                    review = self._map(item, parsed.app_id, page, index)
                except Exception:
                    continue
                if review.review_id in seen_ids:
                    continue
                seen_ids.add(review.review_id)
                reviews.append(review)
                if len(reviews) >= bounded_limit:
                    return reviews

        if not reviews:
            raise CollectionError("评论源返回 0 条数据，请改用 JSON/CSV 导入或稍后重试")
        return reviews[:bounded_limit]

    @staticmethod
    def _label(item: dict[str, Any], key: str, default: Any = None) -> Any:
        value = item.get(key, default)
        return value.get("label", default) if isinstance(value, dict) else value

    @classmethod
    def _map(
        cls, item: dict[str, Any], app_id: str, page: int, index: int
    ) -> Review:
        published_value = cls._label(item, "updated")
        published = datetime.fromisoformat(str(published_value).replace("Z", "+00:00"))
        if published.tzinfo is None:
            published = published.replace(tzinfo=UTC)

        author = item.get("author", {})
        author_name = cls._label(author, "name") if isinstance(author, dict) else None
        return Review(
            review_id=str(cls._label(item, "id", f"apple-{page}-{index}")),
            app_id=app_id,
            storefront="us",
            title=str(cls._label(item, "title", "")),
            content_original=str(cls._label(item, "content", "")),
            rating=int(cls._label(item, "im:rating")),
            app_version=cls._label(item, "im:version"),
            author=author_name,
            published_at=published.astimezone(UTC),
            source="apple-rss:us",
            source_page=page,
        )
