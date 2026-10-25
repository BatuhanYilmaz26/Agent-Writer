import pytest

from fieldnote.services.citations import CitationError, render_citations, validate_citations


def ledger():
    return {
        "src_1": {
            "id": "src_1",
            "url": "https://example.com/article",
            "title": "Evidence [source]",
            "extraction_state": "extracted",
            "retrieved_at": "2026-10-24T00:00:00+00:00",
        }
    }


def test_citations_resolve_from_ledger_without_asserting_factual_support():
    result = validate_citations("Claim [@src_1]. Again [@src_1].", ledger(), require_citations=True)
    assert result.source_ids == ("src_1",) and result.citation_count == 2
    assert not result.support_assessed
    rendered = render_citations("Claim [@src_1].", ledger())
    assert "Claim [1]." in rendered and "https://example.com/article" in rendered
    assert "## References" in rendered and "retrieved 2026-10-24" in rendered


@pytest.mark.parametrize(
    "markdown",
    [
        "Claim [@unknown].",
        "Claim [@bad id].",
        "Claim [@src_1",
        "[link](javascript:alert(1))",
        "![image](https://tracker.example.com/a.png)",
        "<script>alert(1)</script>",
        "[link]\n(javascript:alert(1))",
        "[reference]: javascript:alert(1)",
    ],
)
def test_unknown_markers_and_authored_markup_fail(markdown):
    with pytest.raises(CitationError):
        validate_citations(markdown, ledger())


def test_required_evidence_cannot_be_missing_or_snippet_only():
    with pytest.raises(CitationError, match="must cite"):
        validate_citations("No evidence.", ledger(), require_citations=True)
    sources = ledger()
    sources["src_1"]["extraction_state"] = "snippet"
    with pytest.raises(CitationError, match="extracted"):
        validate_citations("Claim [@src_1].", sources)
