"""Conservative technical-publication review signals, not semantic proof."""

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from fieldnote.domain import CreateRunRequest, ReviewIssue
from fieldnote.services.urls import canonicalize_url

CURRENT = re.compile(r"\b(latest|current|recent|today|newly|güncel|en son|reciente|atual|neueste|aktuell)\b", re.I)
EXECUTION_LABEL = re.compile(r"illustrative|unexecuted|not (?:run|executed)|example only|örnek|çalıştırılma|ilustrativ|no ejecut|não execut|nicht ausgeführt", re.I)
EXPERIENCE = re.compile(r"[^.!?\n]*\b(?:I|we) (?:tested|deployed|measured|benchmarked|ran|built in production)\b[^.!?\n]*[.!?]?", re.I)


def source_kind(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    if host in {"arxiv.org", "export.arxiv.org", "biorxiv.org", "www.biorxiv.org"}:
        return "preprint; peer review not established"
    if host in {"huggingface.co", "github.com"}:
        return "maintainer or community material; check the author"
    if host in {"openai.com", "ai.google.dev", "research.google", "deepmind.google", "openrouter.ai", "anthropic.com", "developers.cloudflare.com"} or host.endswith(".microsoft.com"):
        return "publisher/vendor material; claims need attribution"
    return "unclassified; independence not established"


def source_metadata(sources: dict) -> dict:
    return {key: {**{field: value.get(field) for field in ("title", "url", "published_at", "updated_at", "retrieved_at")},
                  "source_kind": source_kind(value.get("url", "")), "event_at": None}
            for key, value in sources.items()}


def technical_issues(raw: str, request: CreateRunRequest, sources: dict) -> list[ReviewIssue]:
    if request.editorial_profile != "technical-v1":
        return []
    issues = []

    def issue(code: str, text: str) -> None:
        issues.append(ReviewIssue(code=code, severity="error", explanation=text))

    known_urls = {canonicalize_url(s[field]) for s in sources.values()
                  for field in ("url", "original_url", "final_url") if s.get(field)}
    if any(url not in known_urls for url in request.reference_urls):
        issue("selected_source_unavailable", "A selected source was not extracted. Check the source ledger before publishing its claims.")
    if CURRENT.search(request.topic + " " + request.research_context() + " " + raw):
        dates = []
        for source in sources.values():
            try:
                date = datetime.fromisoformat(str(source.get("published_at", "")).replace("Z", "+00:00"))
                if date.tzinfo is not None:
                    dates.append(date.astimezone(UTC))
            except ValueError:
                pass
        now = datetime.now(UTC)
        if len(dates) != len(sources) or not dates or not all(now - timedelta(days=90) <= date <= now + timedelta(minutes=5) for date in dates):
            issue("freshness_unverified", "Current-development wording lacks a recent publication date. Retrieval/update dates alone do not establish a new event; qualify the claim or research it further.")
    for index, block in enumerate(re.finditer(r"(?m)^```[^\n]*\n", raw)):
        if index % 2 == 0 and not EXECUTION_LABEL.search(raw[max(0, block.start() - 260):block.end()]):
            issue("code_execution_unverified", "Label code as illustrative/unexecuted unless you supply its executed result and environment. Fieldnote does not run retrieved examples.")
            break
    notes = re.sub(r"\s+", " ", request.author_notes).casefold()
    for match in EXPERIENCE.finditer(raw):
        claim = re.sub(r"\s+", " ", match[0]).strip().casefold()
        if claim and claim not in notes:
            issue("author_experience_unverified", "A first-person experiment/deployment claim is absent from your observations. Supply your actual experience or remove the claim.")
            break
    return issues
