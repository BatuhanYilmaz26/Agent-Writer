"""On-demand topic ideas from fixed publisher feeds, never a truth verdict."""

from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Any
from xml.etree import ElementTree

import httpx

from fieldnote.services.urls import canonicalize_url

FEEDS = (
    ("Hugging Face", "https://huggingface.co/blog/feed.xml", "publisher/community"),
    ("Google Research", "https://research.google/blog/rss/", "publisher"),
    ("Google DeepMind", "https://deepmind.google/blog/rss.xml", "publisher"),
    ("arXiv machine learning", "https://rss.arxiv.org/rss/cs.LG", "preprint"),
)
MAX_FEED_BYTES = 1_048_576


def _text(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]*>", " ", value))).strip()


def _date(value: str) -> str | None:
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
        except (ValueError, TypeError, OverflowError):
            return None
    if date.tzinfo is None:
        return None
    return date.astimezone(UTC).isoformat()


def parse_feed(raw: bytes, publisher: str, kind: str, retrieved_at: datetime) -> list[dict]:
    # Bound before parsing; disable DTD/entities, including UTF-16 encodings.
    if len(raw) > MAX_FEED_BYTES or b"\x00" in raw or re.search(br"<!\s*(?:DOCTYPE|ENTITY)", raw, re.I):
        raise ValueError("invalid_feed")
    root = ElementTree.fromstring(raw)
    items = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1] in {"item", "entry"}]
    results = []
    for item in items[:100]:
        fields: dict[str, str] = {}
        for node in item:
            key = node.tag.rsplit("}", 1)[-1]
            if key == "link" and node.attrib.get("rel", "alternate") != "alternate":
                continue
            fields[key] = node.attrib.get("href", "") or "".join(node.itertext())
        try:
            url = canonicalize_url(fields.get("link", ""))
        except ValueError:
            continue
        title = _text(fields.get("title", ""))[:240]
        if not title:
            continue
        results.append({
            "id": hashlib.sha256(url.encode()).hexdigest()[:24], "url": url,
            "title": title, "summary": _text(fields.get("description", fields.get("summary", "")))[:800],
            "publisher": publisher, "source_kind": kind,
            "published_at": _date(fields.get("pubDate", fields.get("published", fields.get("date", "")))),
            "updated_at": _date(fields.get("updated", "")),
            "event_at": None, "retrieved_at": retrieved_at.isoformat(),
            "evidence_status": "unreviewed_feed_metadata",
        })
    return results


async def collect_feeds(client: httpx.AsyncClient, *, fixture: bool = False) -> dict[str, Any]:
    now = datetime.now(UTC)
    if fixture:
        return {"fixture": True, "retrieved_at": now.isoformat(), "failures": [], "candidates": [{
            "id": "fixture-topic", "url": "https://example.org/technical-topic-fixture",
            "title": "[Synthetic idea] Evaluating a retrieval system",
            "summary": "A demonstration topic, not a current announcement or factual source.",
            "publisher": "Synthetic fixture", "source_kind": "fixture",
            "published_at": now.isoformat(), "updated_at": None, "event_at": None,
            "retrieved_at": now.isoformat(), "evidence_status": "synthetic",
        }]}

    async def fetch(publisher: str, url: str, kind: str) -> tuple[list[dict], dict | None]:
        try:
            # Fixed HTTPS endpoints; no user fetch URL, redirects, credentials,
            # proxy environment or automatic retry. The caller owns the client.
            async with client.stream("GET", url, follow_redirects=False, timeout=10,
                                     headers={"Accept": "application/rss+xml, application/atom+xml, application/xml"}) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=16_384):
                    body.extend(chunk)
                    if len(body) > MAX_FEED_BYTES:
                        raise ValueError("feed_too_large")
            return parse_feed(bytes(body), publisher, kind, now), None
        except (httpx.HTTPError, ValueError, ElementTree.ParseError):
            return [], {"publisher": publisher, "code": "feed_unavailable_or_invalid"}

    values = await asyncio.gather(*(fetch(*feed) for feed in FEEDS))
    return {"fixture": False, "retrieved_at": now.isoformat(),
            "feeds": [{"publisher": feed[0], "entries": len(items),
                       "status": "unavailable" if failure else "ok" if items else "empty"}
                      for feed, (items, failure) in zip(FEEDS, values, strict=True)],
            "candidates": [item for items, _ in values for item in items],
            "failures": [failure for _, failure in values if failure]}


def rank_topics(snapshot: dict, previous: list[dict], *, days: int = 14, now: datetime | None = None) -> dict:
    if not 1 <= days <= 90:
        raise ValueError("invalid_recent_window")
    now = now or datetime.now(UTC)
    previous_urls = {canonicalize_url(url) for item in previous for url in item.get("reference_urls", [])}
    titles = {_text(str(item.get("topic", ""))).casefold() for item in previous}
    seen: dict[str, dict] = {}
    seen_titles: dict[str, dict] = {}
    for candidate in snapshot.get("candidates", []):
        url = canonicalize_url(candidate["url"])
        title_key = re.sub(r"\W+", " ", candidate["title"].casefold()).strip()
        published = datetime.fromisoformat(candidate["published_at"]) if candidate.get("published_at") else None
        # Updating an old page never turns it into a new research announcement.
        if published and (published > now + timedelta(minutes=5) or published < now - timedelta(days=days)):
            continue
        if url in seen or title_key in seen_titles:
            item = seen.get(url) or seen_titles[title_key]
            item["related_urls"] = list(dict.fromkeys([*item["related_urls"], url]))[:4]
            continue
        prior = url in previous_urls or _text(candidate["title"]).casefold() in titles
        item = {**candidate, "already_drafted": prior, "related_urls": [url],
                "reason": ("Published within your recent window; inspect the original evidence."
                           if published else "Publication date unknown; timeliness is unverified."),
                "warning": "Already used in a saved brief." if prior else ""}
        seen[url] = item
        seen_titles[title_key] = item
    candidates = sorted(seen.values(), key=lambda item: (
        item["already_drafted"], item["published_at"] is None,
        -datetime.fromisoformat(item["published_at"]).timestamp() if item["published_at"] else 0,
        item["title"],
    ))[:24]
    return {**snapshot, "candidates": candidates, "window_days": days,
            "notice": "Publisher-feed ideas, not verified claims. Check event dates in the original source."}
