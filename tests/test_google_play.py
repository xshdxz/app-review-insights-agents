from pathlib import Path

import httpx
import pytest

from app_review_insights.collectors.google_play import GooglePlayCollector
from app_review_insights.errors import CollectionError

FIXTURE = Path(__file__).parent / "fixtures" / "google-play-response.txt"


def _client_for(payload: bytes, status=200):
    calls = {"count": 0}

    def handler(request):
        assert "play.google.com/store/getreviews" in str(request.url)
        # 真实端点翻页耗尽后返回空；第二次请求模拟翻页结束
        calls["count"] += 1
        if calls["count"] > 1:
            return httpx.Response(200, content=b")]}'\n[[null,[],null]]")
        return httpx.Response(status, content=payload)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_parse_play_url():
    collector = GooglePlayCollector()
    parsed = collector.parse_url(
        "https://play.google.com/store/apps/details?id=com.example.app&hl=en_US"
    )
    assert parsed.package == "com.example.app"
    assert parsed.lang == "en_US"


def test_parse_play_url_invalid():
    collector = GooglePlayCollector()
    with pytest.raises(CollectionError):
        collector.parse_url("https://example.com/not-play")


def test_collect_parses_fixture():
    payload = FIXTURE.read_bytes()
    client = _client_for(payload)
    collector = GooglePlayCollector(client=client)
    reviews = collector.collect("https://play.google.com/store/apps/details?id=com.example.app", 10)
    assert len(reviews) == 2
    assert reviews[0].rating == 3
    assert reviews[0].platform == "google-play"
    assert "订阅太贵了" in reviews[0].content_original


def test_collect_empty_is_error():
    client = _client_for(b")]}'\n[[null,[],null]]")
    collector = GooglePlayCollector(client=client)
    with pytest.raises(CollectionError):
        collector.collect("https://play.google.com/store/apps/details?id=com.example.app", 10)
