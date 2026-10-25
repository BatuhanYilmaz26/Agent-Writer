"""Deterministic research bounds, evidence packing and claim-support checks."""

from __future__ import annotations

import hashlib

import pytest

from agentwriter.providers.base import ProviderError
from agentwriter.services.claims import (
    assess_claim,
    extract_claim_assessments,
    unsupported_claim_issues,
)
from agentwriter.services.research import (
    ResearchCoordinator,
    ResearchLimits,
    flag_conflicts,
    pack_evidence,
    rank_results,
)


def excerpt(source_id: str, text: str, *, flags: list[str] | None = None) -> dict:
    digest = hashlib.sha256(text.encode()).hexdigest()
    return {
        "id": "excerpt_" + digest[:12],
        "source_id": source_id,
        "text": text,
        "content_hash": digest,
        "locator": "characters 0:10 of fetched Markdown",
        "flags": flags or [],
    }


class FakeResearch:
    def __init__(self, results: dict[str, list[dict]], extractions: dict[str, dict]) -> None:
        self.results = results
        self.extractions = extractions
        self.searches: list[tuple[str, int]] = []
        self.extracts: list[str] = []

    async def search(self, query: str, max_results: int = 5) -> list[dict]:
        self.searches.append((query, max_results))
        return self.results.get(query, [])[:max_results]

    async def extract(self, url: str) -> dict:
        self.extracts.append(url)
        if url not in self.extractions:
            raise ProviderError("research_extraction_failed")
        return self.extractions[url]


def test_research_limits_are_strictly_bounded() -> None:
    with pytest.raises(ValueError):
        ResearchLimits(max_queries=5)
    with pytest.raises(ValueError):
        ResearchLimits(max_results_per_query=6)
    with pytest.raises(ValueError):
        ResearchLimits(max_pages=9)
    with pytest.raises(ValueError):
        ResearchLimits(max_chars_per_page=999)
    with pytest.raises(ValueError):
        ResearchLimits(max_evidence_tokens=1023)


def test_ranking_prefers_primary_sources_and_spreads_hosts() -> None:
    ranked = rank_results(
        "Python packaging guide",
        [
            {"url": "https://blog.example.org/python-packaging", "title": "Python packaging", "snippet": "guide"},
            {"url": "https://docs.python.org/3/guide", "title": "Python packaging guide", "snippet": "official"},
            {"url": "https://docs.python.org/3/other", "title": "Python packaging other", "snippet": "guide"},
        ],
    )
    assert ranked[0]["url"].startswith("https://docs.python.org")
    assert ranked[1]["url"] != ranked[0]["url"]


@pytest.mark.asyncio
async def test_coordinator_bounds_queries_pages_and_reports_limitations() -> None:
    results: dict[str, list[dict]] = {}
    extractions: dict[str, dict] = {}
    for index in range(12):
        url = f"https://source{index}.example.org/article"
        results.setdefault("query one", []).append(
            {"url": url, "title": f"Topic {index}", "snippet": "relevant topic detail"}
        )
        extractions[url] = {
            "source": {
                "id": f"src_{index}",
                "url": url,
                "original_url": url,
                "final_url": url,
                "title": f"Source {index}",
                "content_hash": "a" * 64,
                "provider": "fixture",
                "published_at": None,
            },
            "excerpt": excerpt(f"src_{index}", f"Relevant topic detail number {index}."),
            "fixture": True,
        }
    results["query one"].insert(
        1,
        {"url": "https://source0.example.org/article", "title": "Duplicate", "snippet": "topic"},
    )
    results["query two"] = [
        {"url": "http://127.0.0.1/private", "title": "Private", "snippet": "topic"}
    ]
    gateway = FakeResearch(results, extractions)
    outcome = await ResearchCoordinator(
        gateway,
        ResearchLimits(max_queries=1, max_results_per_query=5, max_pages=3),
    ).collect(["query one", "query one", "query two"])
    assert len(gateway.searches) == 1
    assert gateway.searches[0] == ("query one", 5)
    assert len(gateway.extracts) == 3
    assert len(outcome.sources) == 3
    assert outcome.truncated is True
    assert outcome.duplicate_urls == 1
    assert any("publication_date_unknown" in item["flags"] for item in outcome.excerpts.values())
    assert outcome.limitations()


@pytest.mark.asyncio
async def test_coordinator_propagates_authentication_and_records_other_failures() -> None:
    class Failing(FakeResearch):
        async def search(self, query: str, max_results: int = 5) -> list[dict]:
            if query == "denied":
                raise ProviderError("research_authentication_failed")
            raise ProviderError("research_timeout", retryable=True)

    gateway = Failing({}, {})
    with pytest.raises(ProviderError, match="research_authentication_failed"):
        await ResearchCoordinator(gateway).collect(["denied"])
    outcome = await ResearchCoordinator(gateway).collect(["transient"])
    assert outcome.failures == [{"stage": "search", "code": "research_timeout"}]
    assert outcome.limitations() == ["1 research page(s) could not be extracted."]


def test_evidence_pack_is_relevant_and_token_bounded() -> None:
    excerpts = {
        "a": excerpt("src_a", "unrelated words " * 100),
        "b": excerpt("src_b", "python packaging guide " * 3),
        "c": excerpt("src_c", "python packaging " * 1000),
    }
    packed = pack_evidence(excerpts, "python packaging guide", max_tokens=40, limit=3)
    assert "b" in packed
    assert "c" not in packed
    assert sum(max(1, (len(item["text"].encode()) + 3) // 4) for item in packed.values()) <= 40


def test_first_oversized_excerpt_cannot_bypass_budget_or_hide_a_smaller_match():
    oversized = excerpt("src_large", "relevant topic " * 1000)
    small = excerpt("src_small", "relevant")
    values = {"large": oversized, "small": small}
    assert pack_evidence(values, "relevant topic", max_tokens=10, limit=1) == {"small": small}
    assert pack_evidence({"large": oversized}, "relevant topic", max_tokens=10) == {}
    assert oversized["content_hash"] == hashlib.sha256(oversized["text"].encode()).hexdigest()


def test_pack_budget_counts_utf8_bytes_and_preserves_exact_boundary():
    assert pack_evidence({"wide": {"text": "é" * 21}}, "é", max_tokens=10) == {}
    exact = {"text": "é" * 20}
    assert pack_evidence({"exact": exact}, "é", max_tokens=10) == {"exact": exact}


@pytest.mark.parametrize("options", [
    {"limit": 0}, {"limit": -1}, {"limit": 25}, {"limit": True},
    {"max_tokens": 0}, {"max_tokens": 12001}, {"max_tokens": True},
])
def test_pack_rejects_invalid_bounds(options):
    with pytest.raises(ValueError):
        pack_evidence({}, "topic", **options)


@pytest.mark.asyncio
async def test_collection_bounds_total_evidence_without_changing_text_hashes_or_inputs():
    pages = {}
    results = []
    for letter, text in (("a", "a" * 2400), ("b", "b" * 2400), ("c", "c" * 1024)):
        url = f"https://example.org/{letter}"
        pages[url] = {
            "source": {"id": f"src_{letter}", "url": url},
            "excerpt": excerpt(f"src_{letter}", text),
        }
        results.append({"url": url, "title": "Topic", "snippet": "topic"})
    gateway = FakeResearch({"topic": results}, pages)
    outcome = await ResearchCoordinator(
        gateway, ResearchLimits(max_pages=3, max_evidence_tokens=1024),
    ).collect(["topic"])
    assert set(outcome.sources) == {"src_a", "src_c"}
    assert outcome.truncated is True and outcome.limitations()
    assert sum((len(item["text"].encode()) + 3) // 4 for item in outcome.excerpts.values()) == 856
    for page in pages.values():
        original = page["excerpt"]
        assert original["flags"] == []
        assert original["content_hash"] == hashlib.sha256(original["text"].encode()).hexdigest()
        if original["id"] in outcome.excerpts:
            selected = outcome.excerpts[original["id"]]
            assert selected["text"] == original["text"]
            assert selected["content_hash"] == original["content_hash"]
            assert selected["locator"] == original["locator"]


@pytest.mark.asyncio
async def test_page_character_limit_drops_oversized_excerpt_without_invalidating_hash():
    url = "https://example.org/large"
    original = excerpt("src_a", "a" * 1001)
    gateway = FakeResearch(
        {"topic": [{"url": url, "title": "Topic"}]},
        {url: {"source": {"id": "src_a"}, "excerpt": original}},
    )
    outcome = await ResearchCoordinator(
        gateway, ResearchLimits(max_chars_per_page=1000),
    ).collect(["topic"])
    assert not outcome.sources and not outcome.excerpts and outcome.truncated
    assert original["text"] == "a" * 1001 and original["flags"] == []
    assert original["content_hash"] == hashlib.sha256(original["text"].encode()).hexdigest()


def test_numeric_conflict_flags_both_excerpts() -> None:
    left = excerpt("src_a", "The release shipped 12 units to the same regional warehouse.")
    right = excerpt("src_b", "The release shipped 15 units to the same regional warehouse.")
    values = {left["id"]: left, right["id"]: right}
    flag_conflicts(values)
    assert all("possible_numeric_conflict" in item["flags"] for item in values.values())


def test_claim_assessment_is_conservative_about_numbers_and_conflicts() -> None:
    supported = excerpt("src_a", "Python 3.13 was released in October 2024 with free-threaded support.")
    assert assess_claim("Python 3.13 was released in October 2024.", [supported])[0] == "supported"
    assert assess_claim("Python 3.14 shipped 99 features.", [supported])[0] == "unsupported"
    conflicting = excerpt("src_b", "Another source disagrees.", flags=["possible_numeric_conflict"])
    assert assess_claim("Python 3.13 was released.", [conflicting])[0] == "conflicting"
    assert assess_claim("Python 3.13 was released.", [])[0] == "unsupported"


def test_claim_extraction_preserves_locators_hashes_and_skips_headings() -> None:
    sources = {"src_a": {"id": "src_a"}}
    excerpts = {"excerpt_1": excerpt("src_a", "Python 3.13 was released in October 2024.")}
    markdown = (
        "# Heading with [@src_a]\n\n"
        "Python 3.13 was released in October 2024 [@src_a]. Unknown [@src_missing].\n\n"
        "Another sentence without a citation.\n"
    )
    claims = extract_claim_assessments(markdown, sources, excerpts)
    assert len(claims) == 1
    claim = claims[0]
    assert claim.assessment == "supported"
    assert claim.claim_locator.startswith("paragraph 2 sentence 1")
    assert claim.excerpt_ids == (excerpts["excerpt_1"]["id"],)
    assert claim.excerpt_hashes == (excerpts["excerpt_1"]["content_hash"],)
    issues = unsupported_claim_issues(
        extract_claim_assessments(
            "A fabricated claim about 2030 [@src_a].",
            sources,
            excerpts,
        )
    )
    assert issues and issues[0]["code"] == "unsupported_claim"
    assert issues[0]["severity"] == "error"
