from pathlib import Path

import httpx
import pytest

from app_review_insights.collectors.social import RedditCollector, SocialCollector

FIXTURE = Path(__file__).parent / "fixtures" / "reddit-search.json"


def _reddit_client(payload: bytes):
    def handler(request):
        assert "reddit.com/search.json" in str(request.url)
        return httpx.Response(200, json=__import__("json").loads(payload))

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_reddit_collect_maps_posts():
    client = _reddit_client(FIXTURE.read_bytes())
    collector = RedditCollector(client=client)
    reviews = collector.collect("some product name", 10)
    assert len(reviews) == 2
    assert reviews[0].platform == "social"
    assert reviews[0].source == "reddit:search"
    assert "订阅太贵了" in reviews[0].content_original
    assert reviews[0].rating == 3  # 无评分约定为中性


def test_reddit_collect_requires_query():
    client = _reddit_client(FIXTURE.read_bytes())
    collector = RedditCollector(client=client)
    with pytest.raises(ValueError):
        collector.collect("", 10)


def test_social_collector_factory():
    collector = SocialCollector()
    assert collector.reddit is not None
