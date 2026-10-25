"""Versioned editorial guidance shared by drafting and saved-revision review."""

TECHNICAL_V1 = (
    " Write concise, natural, professional technical prose for practitioners."
    " Focus on one angle and explain why it matters, with a concrete example,"
    " practical implication and honest limitation. Explain unfamiliar acronyms."
    " Use short paragraphs and only useful headings; avoid hype, canned openings,"
    " engagement bait, repetitive summaries and unsupported superlatives."
    " Treat author_notes as attributed author input, not verified research."
    " Never invent first-person experience, deployments, experiments, benchmarks"
    " or code execution. Label illustrative/unexecuted code as such."
    " Preserve exact model/library versions and dates from the evidence."
    " Distinguish vendor claims, preprints and independent results."
    " State evidence gaps and conflicting/stale claims instead of guessing."
    " Make the title specific to the named technology and the article's actual useful insight."
    " Promise only what the evidence and body deliver; no invented numbers, novelty or clickbait."
    " Answer the reader's main question early, naturally, with enough context to stand alone."
    " Use one H1, descriptive H2 sections, short varied paragraphs, and inline code for identifiers."
    " Use lists, a compact comparison table or a fenced code example only when they clarify the idea."
    " Put citations close to factual claims and retain limitations beside the claims they qualify."
    " Add original analysis from supplied observations, never fabricated experience."
    " Avoid keyword stuffing, repetitive definitions, forced FAQs and padded conclusions."
    " There is no required SEO word count or special AEO/GEO writing formula."
)


def editorial_instruction(profile: str) -> str:
    return TECHNICAL_V1 if profile == "technical-v1" else ""
