"""Deterministic claim-to-excerpt support checks.

Citation existence and claim entailment are separate gates. This module
implements a conservative lexical assessor that also requires numbers and
dates in a claim to appear in its cited excerpts. It is a review signal, not a
truth oracle; unsupported claims block automatic approval.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from agentwriter.domain.languages import fold_for_matching

_CITATION = re.compile(r"\[@([A-Za-z][A-Za-z0-9_.:-]{0,79})\]")
_WORD = re.compile(r"[^\W_](?:[^\W_]|['’-]){1,40}")
_NUMBER = re.compile(r"\b\d+(?:[.,]\d+)?%?\b")
_SECRET = re.compile(
    r"(ignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions|system\s+prompt)", re.I
)
_STOP = frozenset(
    "the and for with from that this into about your you our their its are was were will "
    "have has been being over under more most less least than then when where which while "
    "what who whom whose how why can could should would may might must not but also".split()
)


@dataclass(frozen=True, slots=True)
class ClaimAssessment:
    claim_locator: str
    claim_text: str
    excerpt_ids: tuple[str, ...]
    excerpt_hashes: tuple[str, ...]
    excerpt_locators: tuple[str, ...]
    assessment: str
    score: float
    reason: str

    def as_record(self) -> dict[str, Any]:
        """JSON-compatible record stored beside the exact generated revision."""
        return {
            "claim_locator": self.claim_locator,
            "claim_text": self.claim_text,
            "excerpt_ids": list(self.excerpt_ids),
            "excerpt_hashes": list(self.excerpt_hashes),
            "excerpt_locators": list(self.excerpt_locators),
            "assessment": self.assessment,
            "score": self.score,
            "reason": self.reason,
            "assessor": "lexical-v1",
            "assessor_version": "2",
        }


def _tokens(value: str) -> set[str]:
    normalized = fold_for_matching(value)
    return {word for word in _WORD.findall(normalized) if word not in _STOP}


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)


def _sentences(paragraph: str) -> list[str]:
    sentences: list[str] = []
    for part in re.split(r"(?<=[.!?])\s+", paragraph):
        part = part.strip()
        # Writers commonly place citations after punctuation. Bind leading
        # markers to the preceding sentence rather than discarding them as an
        # empty claim (or incorrectly attaching them to the next sentence).
        leading = re.match(r"^(?:\[@[A-Za-z][A-Za-z0-9_.:-]{0,79}\]\s*)+", part)
        if leading and sentences:
            sentences[-1] += " " + leading[0].strip()
            part = part[leading.end():].strip()
        if part:
            sentences.append(part)
    return sentences


def assess_claim(
    claim_text: str,
    excerpts: list[Mapping[str, Any]],
) -> tuple[str, float, str]:
    """Return supported/unsupported/conflicting with a bounded lexical score."""
    if not excerpts:
        return "unsupported", 0.0, "no_cited_excerpt"
    if any("possible_numeric_conflict" in (_field(excerpt, "flags") or ()) for excerpt in excerpts):
        return "conflicting", 0.0, "cited_excerpts_disagree"
    claim_tokens = _tokens(claim_text)
    if not claim_tokens:
        return "unsupported", 0.0, "empty_claim"
    corpus = " ".join(str(_field(excerpt, "text") or "") for excerpt in excerpts)
    corpus_tokens = _tokens(corpus)
    coverage = len(claim_tokens & corpus_tokens) / len(claim_tokens)
    claim_numbers = set(_NUMBER.findall(claim_text))
    corpus_numbers = set(_NUMBER.findall(corpus))
    missing_numbers = claim_numbers - corpus_numbers
    if missing_numbers:
        return "unsupported", round(coverage, 3), "numeric_claim_not_in_excerpt"
    if _SECRET.search(claim_text):
        return "unsupported", round(coverage, 3), "instruction_like_claim"
    if coverage >= 0.35:
        return "supported", round(coverage, 3), "lexical_overlap"
    return "unsupported", round(coverage, 3), "insufficient_excerpt_support"


def extract_claim_assessments(
    markdown: str,
    sources: Mapping[str, Any],
    excerpts: Mapping[str, Any],
) -> list[ClaimAssessment]:
    """Find cited sentences in an exact Markdown revision and assess each one."""
    if not markdown.strip():
        return []
    claims: list[ClaimAssessment] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for paragraph_index, paragraph in enumerate(re.split(r"\n\s*\n", markdown), start=1):
        if paragraph.lstrip().startswith(("#", ">", "```")):
            continue
        for sentence_index, sentence in enumerate(_sentences(paragraph), start=1):
            ids = tuple(dict.fromkeys(_CITATION.findall(sentence)))
            if not ids:
                continue
            known = tuple(source_id for source_id in ids if source_id in sources)
            if not known:
                continue
            text = _CITATION.sub("", sentence)
            text = re.sub(r"^[\s>*_`#-]+|[\s*_`]+$", "", text).strip()
            if not text:
                continue
            locator = f"paragraph {paragraph_index} sentence {sentence_index}"
            key = (locator, known)
            if key in seen:
                continue
            seen.add(key)
            chosen = [excerpts[excerpt_id] for excerpt_id in excerpts if _field(excerpts[excerpt_id], "source_id") in known]
            assessment, score, reason = assess_claim(text, chosen)
            hashes = tuple(str(_field(excerpt, "content_hash") or "") for excerpt in chosen)
            claims.append(
                ClaimAssessment(
                    claim_locator=locator,
                    claim_text=text[:4000],
                    excerpt_ids=tuple(str(_field(excerpt, "id") or "") for excerpt in chosen),
                    excerpt_hashes=hashes,
                    excerpt_locators=tuple(
                        str(_field(excerpt, "locator") or "")[:500] for excerpt in chosen
                    ),
                    assessment=assessment,
                    score=score,
                    reason=reason,
                )
            )
    return claims


def unsupported_claim_issues(claims: list[ClaimAssessment]) -> list[dict[str, Any]]:
    """Convert non-supported assessments into bounded editorial issues."""
    issues: list[dict[str, Any]] = []
    for claim in claims:
        if claim.assessment == "supported":
            continue
        issues.append(
            {
                "code": "unsupported_claim",
                "severity": "error",
                "explanation": (
                    "A cited factual sentence is not supported by its stored excerpt "
                    f"({claim.reason}). Qualify, research or remove it before approval."
                ),
                "claim_locator": claim.claim_locator,
                "evidence_ids": list(claim.excerpt_ids),
                "action": "Rewrite or remove the unsupported claim.",
            }
        )
    return issues


def token_estimate(value: str) -> int:
    """Conservative character-based estimate; never presented as exact usage."""
    return max(1, math.ceil(len(value.encode("utf-8")) / 4))
