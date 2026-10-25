"""Deterministic citation identity/link checks, distinct from factual review."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from agentwriter.domain.languages import language_profile
from agentwriter.services.urls import validate_public_url

_CITATION = re.compile(r"\[@([A-Za-z][A-Za-z0-9_.:-]{0,79})\]")
_CITATION_LIKE = re.compile(r"\[@([^\]\r\n]*)\]")
_LINK = re.compile(r"!?\[[^\]]*\]\s*\(")
_REFERENCE_LINK = re.compile(r"^\s{0,3}\[[^\]]+\]:", re.MULTILINE)
_HTML = re.compile(r"</?[A-Za-z][^>]*>|<!--", re.IGNORECASE)


class CitationError(ValueError):
    """A draft refers outside the supplied source ledger or uses unsafe markup."""


@dataclass(frozen=True, slots=True)
class CitationValidation:
    source_ids: tuple[str, ...]
    citation_count: int
    support_assessed: bool = False


def _field(source: Any, name: str) -> Any:
    return source.get(name) if isinstance(source, Mapping) else getattr(source, name, None)


def validate_citations(
    markdown: str,
    sources: Mapping[str, Any],
    *,
    require_citations: bool = False,
) -> CitationValidation:
    """Validate source IDs and reject model-authored external links/HTML.

    Generated drafts must use source markers only. The renderer, not a model,
    creates clickable references from the ledger. This checks identity and
    markup, not whether an excerpt supports a claim.
    """
    if len(markdown) > 200_000:
        raise CitationError("Draft exceeds the article size limit")
    if _LINK.search(markdown) or _REFERENCE_LINK.search(markdown) or _HTML.search(markdown):
        raise CitationError("Use source markers instead of authored links or HTML")
    matches = _CITATION.findall(markdown)
    if len(matches) != len(_CITATION_LIKE.findall(markdown)):
        raise CitationError("Malformed source marker")
    if "[@" in _CITATION.sub("", markdown):
        raise CitationError("Unclosed source marker")
    if require_citations and not matches:
        raise CitationError("A researched article must cite its evidence")
    ids = tuple(dict.fromkeys(matches))
    for source_id in ids:
        if source_id not in sources:
            raise CitationError(f"Unknown source marker: {source_id}")
        source = sources[source_id]
        if _field(source, "id") != source_id:
            raise CitationError("Source ledger identity mismatch")
        validate_public_url(_field(source, "url"))
        if _field(source, "extraction_state") not in {"extracted", "fixture"}:
            raise CitationError("A source without extracted evidence cannot support a citation")
    return CitationValidation(ids, len(matches))


def render_citations(markdown: str, sources: Mapping[str, Any], *, language: str = "en") -> str:
    """Resolve validated markers and append references using ledger URLs only."""
    result = validate_citations(markdown, sources)
    profile = language_profile(language)
    if not result.source_ids:
        return markdown
    numbers = {source_id: index + 1 for index, source_id in enumerate(result.source_ids)}
    rendered = _CITATION.sub(lambda match: f"[{numbers[match[1]]}]", markdown)
    references = []
    for source_id in result.source_ids:
        source = sources[source_id]
        title = str(_field(source, "title") or profile.source)
        title = re.sub(r"[\[\]\\\r\n<>]", "", title)[:240]
        url = validate_public_url(_field(source, "url"))
        retrieved = _field(source, "retrieved_at")
        if hasattr(retrieved, "isoformat"):
            retrieved = retrieved.isoformat()
        suffix = f" ({profile.retrieved} {str(retrieved)[:40]})" if retrieved else ""
        references.append(f"{numbers[source_id]}. [{title}](<{url}>){suffix}")
    return rendered.rstrip() + f"\n\n## {profile.references}\n\n" + "\n".join(references) + "\n"
