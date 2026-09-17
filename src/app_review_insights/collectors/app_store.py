from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse
from xml.etree import ElementTree

import httpx

from app_review_insights.collectors.http import get_with_retry
from app_review_insights.errors import CollectionError
from app_review_insights.input_parsing import parse_app_store_url
from app_review_insights.models import Review

_FIRST_PAGE_URL = (
    "https://itunes.apple.com/{storefront}/rss/customerreviews/id={app_id}/json"
    "?urlDesc=/customerreviews/id={app_id}/json?retry=1"
)
_LEGACY_FIRST_PAGE_URL = (
    "https://itunes.apple.com/{storefront}/rss/customerreviews/page=1/"
    "id={app_id}/sortby=mostrecent/json"
)
_PAGE_URL = (
    "https://itunes.apple.com/{storefront}/rss/customerreviews/page={page}/"
    "id={app_id}/sortby=mostrecent/xml?urlDesc=/customerreviews/"
    "page={previous_page}/id={app_id}/sortby=mostrecent/xml"
)
_REVIEWS_PER_PAGE = 50
_MAX_PAGES = 10
_ATOM_NS = "http://www.w3.org/2005/Atom"
_ITUNES_NS = "http://itunes.apple.com/rss"


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
        storefront = parsed.country
        maximum_limit = _REVIEWS_PER_PAGE * _MAX_PAGES
        # Apple RSS caps at 500 reviews; larger targets are bounded silently and
        # the shortfall is disclosed by the run limitations in the UI.
        bounded_limit = max(1, min(limit, maximum_limit))
        reviews: list[Review] = []
        seen_ids: set[str] = set()
        next_url: str | None = _FIRST_PAGE_URL.format(app_id=parsed.app_id, storefront=storefront)
        legacy_fallback_url = _LEGACY_FIRST_PAGE_URL.format(
            app_id=parsed.app_id, storefront=storefront
        )
        used_fallback = False

        for page in range(1, _MAX_PAGES + 1):
            if next_url is None:
                break
            try:
                response = get_with_retry(self.client, next_url)
                response.raise_for_status()
                entries, candidate_next_url = self._parse_page(response)
            except Exception as exc:
                if reviews:
                    return reviews
                raise CollectionError(
                    f"评论采集失败，请稍后重试或改用 JSON/CSV 导入：{exc}"
                ) from exc

            page_reviews = [entry for entry in entries if "im:rating" in entry]
            if not page_reviews and not used_fallback and not reviews:
                # Apple 曾多次调整公开评论接口；主 URL 返回空时回退到旧式第一页，
                # 两者都为空才判定为源端无数据。
                used_fallback = True
                try:
                    response = get_with_retry(self.client, legacy_fallback_url)
                    response.raise_for_status()
                    entries, candidate_next_url = self._parse_page(response)
                except Exception:
                    entries, candidate_next_url = [], None
                page_reviews = [entry for entry in entries if "im:rating" in entry]
            if not page_reviews:
                break

            for index, item in enumerate(page_reviews):
                try:
                    review = self._map(item, parsed.app_id, page, index, storefront)
                except Exception:
                    continue
                if review.review_id in seen_ids:
                    continue
                seen_ids.add(review.review_id)
                reviews.append(review)
                if len(reviews) >= bounded_limit:
                    return reviews

            next_url = self._next_page_url(
                candidate_next_url,
                app_id=parsed.app_id,
                current_page=page,
                storefront=storefront,
            )

        if not reviews:
            raise CollectionError("评论源返回 0 条数据，请改用 JSON/CSV 导入或稍后重试")
        return reviews[:bounded_limit]

    @classmethod
    def _parse_page(cls, response: Any) -> tuple[list[dict[str, Any]], str | None]:
        try:
            payload = response.json()
        except Exception:
            return cls._parse_xml(response.content)

        feed = payload.get("feed", {}) if isinstance(payload, dict) else {}
        entries = feed.get("entry", []) or []
        next_url = cls._next_link(feed.get("link", []))
        return entries, next_url

    @classmethod
    def _parse_xml(cls, content: bytes) -> tuple[list[dict[str, Any]], str | None]:
        root = ElementTree.fromstring(content)
        entries: list[dict[str, Any]] = []
        for element in root.findall(f"{{{_ATOM_NS}}}entry"):
            entries.append(
                {
                    "id": {"label": cls._xml_text(element, "id")},
                    "title": {"label": cls._xml_text(element, "title")},
                    "content": {"label": cls._xml_text(element, "content")},
                    "im:rating": {"label": cls._xml_text(element, "rating", _ITUNES_NS)},
                    "updated": {"label": cls._xml_text(element, "updated")},
                    "author": {
                        "name": {
                            "label": cls._xml_text(
                                element.find(f"{{{_ATOM_NS}}}author"),
                                "name",
                            )
                        }
                    },
                    "im:version": {"label": cls._xml_text(element, "version", _ITUNES_NS)},
                }
            )
        next_url = cls._next_link(
            [{"attributes": link.attrib} for link in root.findall(f"{{{_ATOM_NS}}}link")]
        )
        return entries, next_url

    @staticmethod
    def _xml_text(
        element: ElementTree.Element | None,
        tag: str,
        namespace: str = _ATOM_NS,
    ) -> str:
        if element is None:
            return ""
        child = element.find(f"{{{namespace}}}{tag}")
        return (child.text or "") if child is not None else ""

    @staticmethod
    def _next_link(links: Any) -> str | None:
        if not isinstance(links, list):
            return None
        for link in links:
            if not isinstance(link, dict):
                continue
            attributes = link.get("attributes", link)
            if isinstance(attributes, dict) and attributes.get("rel") == "next":
                return attributes.get("href") or None
        return None

    @staticmethod
    def _safe_next_url(value: str | None) -> str | None:
        if not value:
            return None
        parsed = urlparse(value)
        if parsed.scheme != "https" or parsed.hostname != "itunes.apple.com":
            return None
        return value

    @classmethod
    def _next_page_url(
        cls,
        value: str | None,
        *,
        app_id: str,
        current_page: int,
        storefront: str,
    ) -> str | None:
        safe_value = cls._safe_next_url(value)
        if safe_value is None:
            return None
        if current_page == 1:
            return _PAGE_URL.format(
                page=2,
                previous_page=1,
                app_id=app_id,
                storefront=storefront,
            )
        return safe_value

    @staticmethod
    def _label(item: dict[str, Any], key: str, default: Any = None) -> Any:
        value = item.get(key, default)
        return value.get("label", default) if isinstance(value, dict) else value

    @classmethod
    def _map(
        cls,
        item: dict[str, Any],
        app_id: str,
        page: int,
        index: int,
        storefront: str,
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
            storefront=storefront,
            title=str(cls._label(item, "title", "")),
            content_original=str(cls._label(item, "content", "")),
            rating=int(cls._label(item, "im:rating")),
            app_version=cls._label(item, "im:version"),
            author=author_name,
            published_at=published.astimezone(UTC),
            source=f"apple-rss:{storefront}",
            source_page=page,
        )
