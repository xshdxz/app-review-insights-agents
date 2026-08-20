"""Google Play 评论采集器（尽力而为）。

Google Play 无公开 RSS；使用其网页版评论端点（getreviews），
上游页面结构变化可能导致解析失败，失败时建议改用 JSON/CSV 导入。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from app_review_insights.errors import CollectionError
from app_review_insights.models import Review

_GETREVIEWS_URL = "https://play.google.com/store/getreviews"
_PAGE_SIZE = 100
_MAX_REVIEWS = 1000


@dataclass
class PlayUrl:
    package: str
    lang: str


class GooglePlayCollector:
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

    @staticmethod
    def parse_url(url: str) -> PlayUrl:
        parsed = urlparse(url)
        if parsed.hostname != "play.google.com":
            raise CollectionError("请输入有效的 Google Play 应用链接")
        query = parse_qs(parsed.query)
        package = (query.get("id") or [None])[0]
        if not package:
            raise CollectionError("Google Play 链接缺少 id 参数")
        lang = (query.get("hl") or ["en_US"])[0]
        return PlayUrl(package=package, lang=lang)

    def collect(self, app_url: str, limit: int) -> list[Review]:
        parsed = self.parse_url(app_url)
        bounded = max(1, min(limit, _MAX_REVIEWS))
        reviews: list[Review] = []
        page = 0
        while len(reviews) < bounded and page < 10:
            payload = {
                "authuser": "0",
                "hl": parsed.lang,
                "id": parsed.package,
                "reviewSortOrder": "NEWEST",
                "pageNum": str(page),
                "xhr": "1",
            }
            try:
                response = self.client.post(_GETREVIEWS_URL, data=payload)
                response.raise_for_status()
                rows = self._parse_rows(response.content)
            except Exception as exc:
                if reviews:
                    break
                raise CollectionError(
                    f"Google Play 评论采集失败（该接口为尽力而为）：{exc}"
                ) from exc
            if not rows:
                break
            for row in rows:
                try:
                    review = self._map(row, parsed.package, parsed.lang)
                except Exception:
                    continue
                reviews.append(review)
                if len(reviews) >= bounded:
                    return reviews
            page += 1
        if not reviews:
            raise CollectionError("Google Play 评论源返回 0 条数据，请改用 JSON/CSV 导入")
        return reviews[:bounded]

    @staticmethod
    def _parse_rows(content: bytes) -> list[list[Any]]:
        text = content.decode("utf-8", errors="replace")
        text = text.lstrip(")]}'").strip()
        data = json.loads(text)
        # getreviews 信封结构：[[null, [行1, 行2, ...]], null, [链接...]]
        envelope = data[0] if isinstance(data, list) and data else None
        if not isinstance(envelope, list) or len(envelope) < 2:
            return []
        rows = envelope[1]
        return [row for row in rows if isinstance(row, list)]

    @staticmethod
    def _map(row: list[Any], package: str, lang: str) -> Review:
        # 行结构：[id, [作者, 头像, "", 日期], 评分, null, [标题, [正文], null, [版本], null], null]
        author_block = row[1] if len(row) > 1 else None
        author = author_block[0] if isinstance(author_block, list) and author_block else None
        date_value = (
            author_block[3]
            if isinstance(author_block, list) and len(author_block) > 3
            else ""
        )
        rating_value = row[2] if len(row) > 2 else None
        content_block = row[4] if len(row) > 4 else None
        title = ""
        body = ""
        if isinstance(content_block, list):
            title = content_block[0] or ""
            if len(content_block) > 1 and isinstance(content_block[1], list):
                body = content_block[1][0] or ""
        review_id = row[0] if row[0] else f"gp-{package}-{author}-{date_value}"
        published = _parse_play_date(date_value)
        return Review(
            review_id=str(review_id),
            app_id=package,
            storefront=lang,
            title=str(title),
            content_original=f"{title}\n{body}".strip() or "（无正文）",
            rating=int(rating_value) if isinstance(rating_value, int) else 3,
            published_at=published,
            source="google-play:web",
            platform="google-play",
        )


def _parse_play_date(value: Any) -> datetime:
    text = str(value or "")
    text = re.sub(r"(?:上午|下午|AM|PM|,)", "", text).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%b %d %Y", "%B %d %Y"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return datetime.now(UTC)
