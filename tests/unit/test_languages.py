"""Article-language contracts; fixture checks do not certify model fluency."""

import unicodedata

import pytest
from pydantic import ValidationError

from agentwriter.domain import CreateRunRequest
from agentwriter.domain.languages import LANGUAGES, language_profile
from agentwriter.graph import build_graph, initial_state, make_fixture_dependencies
from agentwriter.services.claims import assess_claim
from agentwriter.services.policy import determine_research
from agentwriter.services.research import pack_evidence
from agentwriter.worker.runner import _prepare_artifact


@pytest.mark.parametrize("language", LANGUAGES)
def test_request_accepts_each_language_and_hashes_it(language):
    request = CreateRunRequest(topic="A forest", language=language)
    assert CreateRunRequest.model_validate_json(request.model_dump_json()).language == language
    if language != "en":
        assert request.request_hash() != CreateRunRequest(topic=request.topic).request_hash()


@pytest.mark.parametrize("language", ["fr", "zh", "pt-BR", "EN", "", None, 1])
def test_unconfigured_language_is_rejected(language):
    with pytest.raises(ValidationError):
        CreateRunRequest(topic="A forest", language=language)


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.asyncio
async def test_localized_fixture_and_worker_artifact_preserve_disclosures(language):
    request = CreateRunRequest(topic="A forest", language=language, target_words=500)
    result = await build_graph(make_fixture_dependencies(request, max_review_rounds=0)).compile().ainvoke(initial_state())
    profile = language_profile(language)
    assert result["outcome"] == "needs_review"
    assert profile.fixture_sections[0] in result["assembled_markdown"]
    assert profile.fixture_sentence.strip() in result["assembled_markdown"]
    assert f"## {profile.references}" in result["final_markdown"]
    assert result["final_markdown"].startswith(f"> {profile.synthetic_notice}")
    assert not any(issue["code"] == "word_budget_deviation" for issue in result["issues"])
    prepared = _prepare_artifact(dict(result), language=language, failure_code="interrupted")
    assert prepared is not None
    assert prepared["markdown"] == result["final_markdown"]
    assert prepared["citation_map"]["language"] == language
    assert any(issue["code"] == "interrupted" for issue in prepared["issues"])


@pytest.mark.parametrize("language,topic", [
    ("en", "Write a poem about a forest"),
    ("tr", "Bir orman hakkında kısa bir öykü"),
    ("es", "Un cuento sobre un bosque"),
    ("pt", "Um poema sobre a floresta"),
    ("de", "Ein Gedicht über den Wald"),
])
def test_native_creative_brief_can_disable_research(language, topic):
    assert not determine_research(CreateRunRequest(topic=topic, language=language, research_mode="off")).need_research


@pytest.mark.parametrize("language,topic", [
    ("tr", "Güncel vergi yasalarını açıkla"),
    ("es", "Explica los precios actuales"),
    ("pt", "Explique os preços atuais"),
    ("pt", "Explique os resultados atuais"),
    ("de", "Erkläre die aktuellen Steuergesetze"),
    ("de", "Beschreibe die Medikamentendosierung"),
])
def test_native_current_facts_cannot_disable_research(language, topic):
    with pytest.raises(ValidationError, match="research_required"):
        CreateRunRequest(topic=topic, language=language, research_mode="off")
    assert determine_research(CreateRunRequest(topic=topic, language=language, research_mode="auto")).need_research


@pytest.mark.parametrize("text,other", [
    ("çağrı", "cagri"), ("año", "ano"), ("avó", "avo"), ("schön", "schon"),
])
def test_evidence_tokens_preserve_accents_and_normalize_unicode(text, other):
    decomposed = unicodedata.normalize("NFD", text)
    assert assess_claim(text, [{"text": decomposed}])[0] == "supported"
    assert assess_claim(text, [{"text": other}])[0] == "unsupported"
    values = {"wrong": {"text": other}, "right": {"text": decomposed}}
    assert list(pack_evidence(values, text, max_tokens=10, limit=1)) == ["right"]


def test_language_registry_rejects_an_unknown_profile():
    with pytest.raises(ValueError, match="Choose English"):
        language_profile("fr")


@pytest.mark.parametrize("claim,evidence", [("İSTANBUL", "istanbul"), ("Straße", "STRASSE")])
def test_unicode_casefolding_preserves_complete_words(claim, evidence):
    assert assess_claim(claim, [{"text": evidence}])[0] == "supported"
    assert list(pack_evidence({"correct": {"text": evidence}}, claim, max_tokens=10)) == ["correct"]
