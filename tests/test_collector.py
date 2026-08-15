from xml.sax.saxutils import escape

from app_review_insights.collectors.app_store import AppStoreCollector
from app_review_insights.errors import CollectionError


def rss_payload(*entries, next_url: str | None = None):
    feed = {"entry": list(entries)}
    if next_url:
        feed["link"] = [{"attributes": {"rel": "next", "href": next_url}}]
    return {"feed": feed}


def rss_review(review_id: str, content: str, rating: str = "2"):
    return {
        "id": {"label": review_id},
        "title": {"label": "Pricing"},
        "content": {"label": content},
        "im:rating": {"label": rating},
        "updated": {"label": "2026-08-01T10:00:00-07:00"},
        "author": {"name": {"label": "reviewer"}},
        "im:version": {"label": "8.5.0"},
    }


def atom_xml(*entries, next_url: str | None = None) -> bytes:
    next_link = f'<link rel="next" href="{escape(next_url)}" />' if next_url else ""
    rendered_entries = "".join(
        (
            "<entry>"
            f"<id>{escape(entry['id']['label'])}</id>"
            f"<title>{escape(entry['title']['label'])}</title>"
            f"<content>{escape(entry['content']['label'])}</content>"
            f"<im:rating>{escape(entry['im:rating']['label'])}</im:rating>"
            f"<updated>{escape(entry['updated']['label'])}</updated>"
            "<author>"
            f"<name>{escape(entry['author']['name']['label'])}</name>"
            "</author>"
            f"<im:version>{escape(entry['im:version']['label'])}</im:version>"
            "</entry>"
        )
        for entry in entries
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom" '
        'xmlns:im="http://itunes.apple.com/rss">'
        f"{next_link}{rendered_entries}</feed>"
    ).encode()


class FakeResponse:
    def __init__(
        self,
        payload=None,
        status_code: int = 200,
        *,
        content: bytes | None = None,
    ):
        self.payload = payload
        self.status_code = status_code
        self.content = content or b""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        if self.payload is None:
            raise ValueError("response is not JSON")
        return self.payload


class FakeHttpClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requested_urls = []

    def get(self, url):
        self.requested_urls.append(url)
        return self.responses.pop(0)


def test_collector_maps_rss_data_to_reviews():
    client = FakeHttpClient(
        [FakeResponse(rss_payload(rss_review("123", "The renewal date is unclear.")))]
    )
    collector = AppStoreCollector(client=client)

    reviews = collector.collect(
        "https://apps.apple.com/us/app/workout-for-women-home-gym/id839285684",
        limit=20,
    )

    assert reviews[0].review_id == "123"
    assert reviews[0].storefront == "us"
    assert reviews[0].app_version == "8.5.0"
    assert reviews[0].source == "apple-rss:us"
    assert reviews[0].source_page == 1
    assert client.requested_urls == [
        "https://itunes.apple.com/us/rss/customerreviews/id=839285684/json"
        "?urlDesc=/customerreviews/id=839285684/json?retry=1"
    ]


def test_default_client_uses_apple_compatible_browser_user_agent():
    collector = AppStoreCollector()

    assert collector.client.headers["user-agent"].startswith("Mozilla/5.0")


def test_collector_paginates_and_removes_duplicate_review_ids():
    page_one = [rss_review(f"r-{index}", f"Review {index}") for index in range(50)]
    page_two = [rss_review("r-49", "Duplicate across pages"), rss_review("r-50", "New review")]
    feed_next_url = (
        "https://itunes.apple.com/us/rss/customerreviews/page=2/"
        "id=839285684/sortby=mostrecent/xml?urlDesc=/customerreviews/"
        "id=839285684/json"
    )
    working_next_url = (
        "https://itunes.apple.com/us/rss/customerreviews/page=2/"
        "id=839285684/sortby=mostrecent/xml?urlDesc=/customerreviews/"
        "page=1/id=839285684/sortby=mostrecent/xml"
    )
    client = FakeHttpClient(
        [
            FakeResponse(rss_payload(*page_one, next_url=feed_next_url)),
            FakeResponse(content=atom_xml(*page_two)),
        ]
    )

    reviews = AppStoreCollector(client=client).collect(
        "https://apps.apple.com/us/app/example/id839285684",
        limit=51,
    )

    assert len(reviews) == 51
    assert reviews[-1].review_id == "r-50"
    assert reviews[-1].source_page == 2
    assert len(client.requested_urls) == 2
    assert client.requested_urls[-1] == working_next_url


def test_collector_does_not_follow_non_apple_next_link():
    page_one = [rss_review(f"r-{index}", f"Review {index}") for index in range(50)]
    client = FakeHttpClient(
        [
            FakeResponse(
                rss_payload(
                    *page_one,
                    next_url="https://example.com/untrusted-reviews.xml",
                )
            )
        ]
    )

    reviews = AppStoreCollector(client=client).collect(
        "https://apps.apple.com/us/app/example/id839285684",
        limit=100,
    )

    assert len(reviews) == 50
    assert client.requested_urls == [
        "https://itunes.apple.com/us/rss/customerreviews/id=839285684/json"
        "?urlDesc=/customerreviews/id=839285684/json?retry=1"
    ]


def test_collector_wraps_upstream_errors_with_fallback_guidance():
    client = FakeHttpClient([FakeResponse({}, status_code=503)])

    try:
        AppStoreCollector(client=client).collect(
            "https://apps.apple.com/us/app/example/id839285684",
            limit=20,
        )
    except CollectionError as exc:
        assert "JSON/CSV" in str(exc)
    else:
        raise AssertionError("expected CollectionError")


def test_collector_returns_completed_pages_when_a_later_page_fails():
    page_one = [rss_review(f"r-{index}", f"Review {index}") for index in range(50)]
    next_url = (
        "https://itunes.apple.com/us/rss/customerreviews/page=2/id=839285684/sortby=mostrecent/xml"
    )
    client = FakeHttpClient(
        [
            FakeResponse(rss_payload(*page_one, next_url=next_url)),
            FakeResponse({}, status_code=503),
        ]
    )

    reviews = AppStoreCollector(client=client).collect(
        "https://apps.apple.com/us/app/example/id839285684",
        limit=100,
    )

    assert len(reviews) == 50
    assert len(client.requested_urls) == 2


def test_collector_skips_a_malformed_entry_without_losing_valid_reviews():
    malformed = rss_review("bad", "Bad date")
    malformed["updated"] = {"label": "not-a-date"}
    client = FakeHttpClient(
        [
            FakeResponse(
                rss_payload(
                    rss_review("good-1", "First valid review"),
                    malformed,
                    rss_review("good-2", "Second valid review"),
                )
            )
        ]
    )

    reviews = AppStoreCollector(client=client).collect(
        "https://apps.apple.com/us/app/example/id839285684",
        limit=20,
    )

    assert [review.review_id for review in reviews] == ["good-1", "good-2"]


def test_collector_rejects_limits_above_apple_rss_capacity():
    collector = AppStoreCollector(client=FakeHttpClient([]))

    try:
        collector.collect(
            "https://apps.apple.com/us/app/example/id839285684",
            limit=501,
        )
    except CollectionError as exc:
        assert "最多 500" in str(exc)
        assert "JSON/CSV" in str(exc)
    else:
        raise AssertionError("expected CollectionError")


def test_collector_rejects_empty_feed():
    client = FakeHttpClient([FakeResponse(rss_payload())])

    try:
        AppStoreCollector(client=client).collect(
            "https://apps.apple.com/us/app/example/id839285684",
            limit=20,
        )
    except CollectionError as exc:
        assert "0 条" in str(exc)
    else:
        raise AssertionError("expected CollectionError")
