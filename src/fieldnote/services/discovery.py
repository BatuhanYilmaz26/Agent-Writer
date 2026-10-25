"""Revision-specific editorial signals, not ranking predictions or truth verdicts."""

import re

HYPE = re.compile(r"\b(game[- ]changer|revolutionary|ultimate|you won't believe|guaranteed|"
                  r"changes everything|secret to|mind[- ]blowing)\b", re.I)
CANNED = re.compile(r"\b(in today's (?:rapidly evolving|fast-paced)|in the ever-evolving|"
                    r"in this article,? we (?:will|shall)|let's delve|unlock the power)\b", re.I)


def plain_inline(text: str) -> str:
    text = re.sub(r"\[@[^\]]+\]", "", text)
    parts = []
    for part in re.split(r"(`[^`\n]+`)", text):
        if re.fullmatch(r"`[^`\n]+`", part):
            parts.append(part[1:-1])
        else:
            part = re.sub(r"\*\*([^*\n]+)\*\*", r"\1", part)
            parts.append(re.sub(r"(?<!\w)\*([^*\n]+)\*", r"\1", part))
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def article_discovery(markdown: str, options: list | tuple = ()) -> dict:
    """Count visible prose only; leave code, quotes and table cells out of prose warnings.

    Thresholds are local editorial suggestions, never search-engine requirements.
    English phrase detection is deliberately labeled rather than applied as a
    language-quality verdict to all five supported languages.
    """
    prose: list[str] = []
    headings: list[tuple[int, str]] = []
    paragraph: list[str] = []
    in_code = False

    def flush() -> None:
        if paragraph:
            prose.append(plain_inline(" ".join(paragraph)))
            paragraph.clear()

    for line in markdown.splitlines():
        if line.startswith("```"):
            flush()
            in_code = not in_code
        elif in_code:
            continue
        elif heading := re.match(r"^(#{1,6})\s+(.+)$", line):
            flush()
            headings.append((len(heading[1]), plain_inline(heading[2])))
        elif not line.strip() or re.match(r"^\s*(?:>|[-*] |\d+[.)] |\|)", line):
            flush()
        else:
            paragraph.append(line.strip())
    flush()
    titles = [text for level, text in headings if level == 1]
    title = titles[0] if titles else ""
    tips = []
    if len(titles) != 1:
        tips.append("Use one descriptive article title (a single # heading).")
    if len(title) > 90:
        tips.append("Consider a tighter title with the main subject early. Search display width varies; there is no fixed character cutoff.")
    if HYPE.search(title):
        tips.append("Possible English hype in the title: replace it with the specific insight the article supports.")
    if re.search(r"\d", title):
        tips.append("Check every number, version and date in the title against the body and primary sources.")
    long = sum(len(p.split()) > 90 for p in prose)
    if long:
        tips.append(f"Consider splitting {long} prose paragraph(s) longer than 90 words at a natural change of idea.")
    if sum(len(p.split()) for p in prose) >= 300 and not any(level == 2 for level, _ in headings):
        tips.append("Add descriptive section headings where they help a reader scan this longer draft.")
    if any(level > previous + 1 for (previous, _), (level, _) in zip(headings, headings[1:], strict=False)):
        tips.append("Keep heading levels in sequence so the article outline is easy to navigate.")
    if CANNED.search(" ".join(prose)):
        tips.append("Possible canned English phrasing: open with the concrete problem or finding in your own voice.")
    first = prose[0] if prose else ""
    sentences = re.split(r"(?<=[.!?])\s+", first)
    description = sentences[0] if sentences and len(sentences[0]) <= 500 else ""
    return {
        "version": "editorial-discovery-v1", "title": title,
        "headline_options": list(dict.fromkeys(str(o) for o in options if isinstance(o, str) and o.strip()))[:5],
        "description_suggestion": description, "title_characters": len(title),
        "long_paragraphs": long, "tips": tips,
        "reminder": "Check the title's promise, direct opening answer, source dates and your own useful perspective. These are editorial signals, not SEO scores or factual approval.",
    }
