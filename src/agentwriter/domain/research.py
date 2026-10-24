"""Deterministic research boundaries shared by validation and execution.

These deliberately conservative rules are application policy, not a classifier
claim. Ambiguous factual briefs stay researched rather than guessing that a
named product or a number is timeless.
"""

import re

_FRESH = re.compile(
    r"\b(latest|current|currently|today|yesterday|tomorrow|recent|recently|"
    r"news|this\s+(?:week|month|year)|up[- ]to[- ]date|pricing|prices?|costs?|"
    r"rates?|statistics?|benchmarks?|versions?|releases?|regulations?|laws?|"
    r"legislation|legal|medical|dosage|clinical|investment|financial|tax)\b",
    re.IGNORECASE,
)
_SOURCES = re.compile(
    r"\b(sources?|citations?|cite|references?|evidence|verify|fact[- ]check|"
    r"official\s+(?:docs?|documentation|information)|research)\b",
    re.IGNORECASE,
)
_NUMBER = re.compile(r"\d")
_NAMED = re.compile(
    r"\b(?:[A-Z]{2,}[A-Za-z0-9]*|[A-Z][a-z]+[A-Z][A-Za-z0-9]*|"
    r"iPhone|iPad|macOS|Windows|Linux|Samsung|Google|Microsoft|Anthropic|"
    r"Gemini|Claude|Tavily|Treg|OpenRouter|Docker|Python|JavaScript|"
    r"PostgreSQL|FastAPI|LangGraph)\b"
)
_CREATIVE = re.compile(
    r"\b(poem|poetry|fiction|fictional|fantasy|fable|fairy\s+tale|"
    r"bedtime\s+story|creative\s+story|short\s+story|imaginary|invented)\b",
    re.IGNORECASE,
)
_GENERAL = re.compile(
    r"^(?:explain|describe|introduce|understand|understanding|introduction\s+to|"
    r"how|why|what\s+is|what\s+are|the\s+basics\s+of|a\s+beginner['’]?s?\s+guide\s+to)\b",
    re.IGNORECASE,
)


def factual_research_reason(topic: str, instructions: str = "") -> str | None:
    """Return the first non-overridable research reason, if one is explicit."""
    text = f"{topic} {instructions}"
    if _SOURCES.search(text):
        return "explicit_sources"
    if _FRESH.search(text):
        return "current_or_sensitive_facts"
    if _NUMBER.search(text):
        return "numeric_claims"
    # Skip the initial word to avoid treating a sentence's initial capital as a
    # named entity. Known brands/acronyms remain checked in the whole brief.
    if _NAMED.search(text):
        return "named_product_or_organization"
    return None


def is_general_or_creative(topic: str, instructions: str = "") -> bool:
    """Only recognize explicit creative or general explanatory drafting."""
    text = f"{topic} {instructions}"
    return bool(_CREATIVE.search(text) or _GENERAL.search(topic))
