"""Feed parsing/freshness failure cases use synthetic metadata only."""

from datetime import UTC, datetime

import httpx
import pytest

from agentwriter.services.topics import (
    FEEDS,
    MAX_FEED_BYTES,
    collect_feeds,
    parse_feed,
    rank_topics,
)

NOW = datetime(2026, 10, 25, tzinfo=UTC)
RSS = b'''<rss><channel><item><title>Model release</title><link>https://example.org/release?utm_source=test</link><pubDate>Sat, 24 Oct 2026 12:00:00 GMT</pubDate><description>Vendor claim, not a benchmark.</description></item></channel></rss>'''


def test_rss_atom_dates_and_untrusted_markup():
    item = parse_feed(RSS, "Publisher", "publisher", NOW)[0]
    assert item["url"] == "https://example.org/release"
    assert item["published_at"] == "2026-10-24T12:00:00+00:00"
    assert item["event_at"] is None and item["updated_at"] is None
    atom = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>&lt;b&gt;A paper&lt;/b&gt;</title><link href="https://example.org/paper"/><published>2026-01-01T00:00:00Z</published><updated>2026-10-25T00:00:00Z</updated></entry></feed>'''
    paper = parse_feed(atom, "Paper", "preprint", NOW)[0]
    assert paper["title"] == "A paper" and paper["updated_at"].startswith("2026-10-25")
    assert rank_topics({"candidates": [paper]}, [], now=NOW)["candidates"] == []
    assert parse_feed(RSS.replace(b"https://example.org/release?utm_source=test", b"http://localhost/private"), "x", "publisher", NOW) == []


@pytest.mark.parametrize("raw", [b'<!DOCTYPE rss [<!ENTITY x "boom">]><rss/>', b"x" * (MAX_FEED_BYTES + 1), "<rss/>".encode("utf-16")], ids=["entities", "oversized", "utf16"])
def test_feed_entity_and_size_rejection(raw):
    with pytest.raises(ValueError):
        parse_feed(raw, "x", "publisher", NOW)


def test_freshness_deduplication_and_covered_topic_warning():
    item = parse_feed(RSS, "Publisher", "publisher", NOW)[0]
    duplicate = {**item, "url": "https://example.org/mirror"}
    future = {**item, "title": "Future", "url": "https://example.org/future", "published_at": "2027-01-01T00:00:00+00:00"}
    unknown = {**item, "title": "Undated", "url": "https://example.org/undated", "published_at": None}
    result = rank_topics({"candidates": [item, duplicate, future, unknown]}, [{"reference_urls": [item["url"]]}], now=NOW)
    assert len(result["candidates"]) == 2
    covered = next(c for c in result["candidates"] if c["title"] == item["title"])
    assert covered["already_drafted"] and len(covered["related_urls"]) == 2
    assert "unknown" in next(c for c in result["candidates"] if c["title"] == "Undated")["reason"]


async def test_collection_is_fixed_bounded_and_reports_partial_failures():
    calls = []

    def response(request):
        calls.append(str(request.url))
        assert "authorization" not in request.headers and "cookie" not in request.headers
        if str(request.url) == FEEDS[1][1]:
            return httpx.Response(302, headers={"Location": "http://localhost/secret"})
        return httpx.Response(200, content=RSS)

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        result = await collect_feeds(client)
    assert set(calls) == {feed[1] for feed in FEEDS} and len(calls) == 4
    assert len(result["candidates"]) == 3 and len(result["failures"]) == 1
    assert not result["fixture"]
