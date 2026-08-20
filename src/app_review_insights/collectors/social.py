"""社交舆情采集（尽力而为）：Reddit 搜索完整实现；X 需要可配置端点。

结果标记 platform="social"、source="reddit:search"/"x:search"，
RAG 默认不检索社交语料（避免噪声污染证据链），仅显式开启时参与。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from app_review_insights.errors import CollectionError
from app_review_insights.models import Review


class RedditCollector:
    def __init__(self, client: Any | None = None, timeout_seconds: float = 20):
        self.client = client or httpx.Client(
            timeout=timeout_seconds,
            headers={"User-Agent": "product-intel-agent/1.0 (research)"},
            follow_redirects=True,
        )

    def collect(self, query: str, limit: int) -> list[Review]:
        query = query.strip()
        if not query:
            raise ValueError("Reddit 采集需要查询词（产品名）")
        response = self.client.get(
            "https://www.reddit.com/search.json",
            params={"q": query, "limit": min(max(limit, 1), 100), "sort": "relevance"},
        )
        response.raise_for_status()
        payload = response.json()
        children = payload.get("data", {}).get("children", [])
        reviews: list[Review] = []
        for child in children:
            item = child.get("data", {})
            post_id = item.get("id", "")
            title = item.get("title", "")
            body = item.get("selftext", "")
            content = f"{title}\n{body}".strip()
            if not content:
                continue
            created = datetime.fromtimestamp(float(item.get("created_utc", 0)), tz=UTC)
            reviews.append(
                Review(
                    review_id=f"reddit-{post_id}" if post_id else f"reddit-{len(reviews)}",
                    app_id="social",
                    content_original=content,
                    rating=3,  # 无评分约定为中性
                    published_at=created,
                    source="reddit:search",
                    platform="social",
                    author=item.get("author"),
                )
            )
        if not reviews:
            raise CollectionError("Reddit 搜索没有返回结果")
        return reviews[:limit]


class XCollector:
    """X 舆情采集：需要用户配置 SOCIAL_X_ENDPOINT（返回 JSON 数组）。

    端点响应格式：[{"id": "...", "text": "...", "author": "...", "created_at": "ISO8601"}]
    """

    def __init__(self, endpoint: str, client: Any | None = None, timeout_seconds: float = 20):
        if not endpoint:
            raise ValueError("未配置 SOCIAL_X_ENDPOINT，无法采集 X 舆情")
        self.endpoint = endpoint
        self.client = client or httpx.Client(timeout=timeout_seconds)

    def collect(self, query: str, limit: int) -> list[Review]:
        response = self.client.get(self.endpoint, params={"q": query, "limit": limit})
        response.raise_for_status()
        items = response.json()
        if not isinstance(items, list):
            raise CollectionError("X 端点应返回 JSON 数组")
        reviews: list[Review] = []
        for item in items[:limit]:
            text = item.get("text", "").strip()
            if not text:
                continue
            created = item.get("created_at")
            published = (
                datetime.fromisoformat(created.replace("Z", "+00:00"))
                if created
                else datetime.now(UTC)
            )
            reviews.append(
                Review(
                    review_id=f"x-{item.get('id', len(reviews))}",
                    app_id="social",
                    content_original=text,
                    rating=3,
                    published_at=published,
                    source="x:search",
                    platform="social",
                    author=item.get("author"),
                )
            )
        if not reviews:
            raise CollectionError("X 端点没有返回结果")
        return reviews


class SocialCollector:
    """按配置分发到 Reddit / X。"""

    def __init__(self, x_endpoint: str = "", client: Any | None = None):
        self.reddit = RedditCollector(client=client)
        self.x_endpoint = x_endpoint
        self.x: XCollector | None = None
        if x_endpoint:
            self.x = XCollector(x_endpoint, client=client)
