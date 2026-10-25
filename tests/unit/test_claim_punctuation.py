"""Trailing citation punctuation must never hide an unsupported sentence."""

from agentwriter.services.claims import extract_claim_assessments


def test_post_punctuation_citations_bind_previous_claim_not_empty_or_next_text():
    sources = {"source": {"id": "source"}}
    excerpts = {"excerpt": {"id": "excerpt", "source_id": "source", "text": "The model reaches 90% accuracy.",
                            "content_hash": "a" * 64, "locator": "paragraph 1"}}
    text = "The model reaches 95% accuracy. [@source] The model reaches 90% accuracy. [@source]"
    claims = extract_claim_assessments(text, sources, excerpts)
    assert len(claims) == 2
    assert claims[0].claim_text == "The model reaches 95% accuracy."
    assert claims[0].assessment == "unsupported" and claims[0].reason == "numeric_claim_not_in_excerpt"
    assert claims[1].claim_text == "The model reaches 90% accuracy."
    assert claims[1].assessment == "supported"
    assert claims[0].as_record()["assessor_version"] == "2"


def test_multiple_trailing_markers_keep_both_excerpt_identities():
    sources = {key: {"id": key} for key in ("one", "two")}
    excerpts = {key: {"id": key, "source_id": key, "text": "A model is an approximation.",
                      "content_hash": char * 64, "locator": key}
                for key, char in (("one", "a"), ("two", "b"))}
    claims = extract_claim_assessments("A model is an approximation. [@one] [@two]", sources, excerpts)
    assert len(claims) == 1 and claims[0].excerpt_ids == ("one", "two")
    assert claims[0].excerpt_hashes == ("a" * 64, "b" * 64)
