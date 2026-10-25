"""Editorial signals supplement reading without manufacturing search scores."""

import io
import json
import zipfile

import pytest
from pydantic import ValidationError

from agentwriter.domain import ContentPlan, CreateRunRequest, markdown_hash
from agentwriter.domain.publishing import PublicationRequest
from agentwriter.graph import build_graph, initial_state, make_fixture_dependencies
from agentwriter.services.discovery import article_discovery
from agentwriter.services.publishing import (
    article_html,
    build_publication,
    plain_markdown,
    publication_zip,
)
from agentwriter.worker.runner import _prepare_artifact


def test_readability_notes_count_prose_without_penalizing_literal_code_or_quotes():
    long = "practitioner " * 100
    markdown = f"# The ultimate model in 2026\n\nIn today's rapidly evolving landscape.\n\n{long}\n\n```python\n{long * 4}\n```\n\n> {long}\n"
    result = article_discovery(markdown)
    assert result["long_paragraphs"] == 1
    assert any("hype" in tip for tip in result["tips"])
    assert any("number" in tip for tip in result["tips"])
    assert any("canned" in tip for tip in result["tips"])
    assert "score" not in result
    assert result["description_suggestion"] == "In today's rapidly evolving landscape."


def test_headlines_are_optional_bounded_and_preserved_in_artifact():
    payload = {"title": "A precise title", "target_words": 200, "tasks": [
        {"id": "a", "order": 1, "title": "Problem", "goal": "Explain", "target_words": 100},
        {"id": "b", "order": 2, "title": "Implication", "goal": "Apply", "target_words": 100},
    ]}
    assert ContentPlan.model_validate(payload).headline_options == ()
    options = ["RAG evaluation: what similarity misses", "Why similarity is not answer quality", "How should you evaluate a RAG answer?"]
    plan = ContentPlan.model_validate({**payload, "headline_options": options})
    artifact = _prepare_artifact({"assembled_markdown": "# A precise title\n\nAn example.", "plan": plan.model_dump(mode="json")})
    assert artifact and artifact["citation_map"]["headline_options"] == options
    for invalid in (["One\nTwo"], ["A"] * 6):
        with pytest.raises(ValidationError):
            ContentPlan.model_validate({**payload, "headline_options": invalid})


async def test_title_alternatives_share_the_existing_plan_call_and_reach_the_article():
    request = CreateRunRequest(topic="RAG evaluation", target_words=350, editorial_profile="technical-v1")
    deps = make_fixture_dependencies(request, max_review_rounds=0)
    options = ["RAG evaluation: what similarity misses", "Why retrieval is only part of RAG evaluation", "How should you check a RAG answer?"]
    deps.model._responses["plan"]["headline_options"] = options
    result = await build_graph(deps).compile().ainvoke(initial_state())
    assert result["plan"]["headline_options"] == options
    assert result["model_calls"] == 4
    assert deps.model.calls == ["plan", "section:section_1:0", "section:section_2:0", "review:0"]
    artifact = _prepare_artifact(dict(result))
    assert artifact and artifact["citation_map"]["headline_options"] == options


@pytest.mark.parametrize("title", ["Türkçe teknik yazı", "Una explicación útil", "Uma comparação prática", "Ein präziser Überblick"])
def test_metadata_and_notes_preserve_language_without_english_only_verdict(title):
    result = article_discovery(f"# {title}\n\n{title}.\n\n## Context\n\nA short paragraph.")
    assert result["title"] == title and not result["tips"]


def test_discovery_export_binds_metadata_to_revision_and_retains_old_package_compatibility():
    body = "# RAG evaluation\n\nSimilarity does not establish answer quality.\n"
    article = {"id": "revision-a", "content_hash": markdown_hash(body), "markdown": body,
               "citation_map": {"headline_options": ["UNSELECTED_PRIVATE_998"]}}
    request = PublicationRequest(content_hash=article["content_hash"], search_title="RAG evaluation: checking answer quality", search_description="A practical distinction, with limitations.")
    data = build_publication(article, request, {"destinations": ["medium"], "author_notes": "PRIVATE_DO_NOT_EXPORT"})
    assert data["discovery"]["search_title"] == request.search_title
    package = {"id": "package-a", "created_at": data["created_at"], "data": data}
    with zipfile.ZipFile(io.BytesIO(publication_zip(package))) as archive:
        metadata = json.loads(archive.read("discovery.json"))
        assert metadata["content_hash"] == article["content_hash"] and metadata["revision_id"] == "revision-a"
        assert metadata["search_description"] == request.search_description
        assert all(b"PRIVATE_DO_NOT_EXPORT" not in archive.read(n) for n in archive.namelist())
        assert all(b"UNSELECTED_PRIVATE_998" not in archive.read(n) for n in archive.namelist())
    del data["discovery"]
    with zipfile.ZipFile(io.BytesIO(publication_zip(package))) as archive:
        assert "discovery.json" not in archive.namelist()
        assert b"discovery.json" not in archive.read("HANDOFF.txt")
    for values in ({"search_title": "One\nTwo"}, {"search_description": "\ud800"}, {"search_title": " "}):
        with pytest.raises(ValidationError):
            PublicationRequest(content_hash=article["content_hash"], **values)


def test_reading_export_formats_technical_blocks_without_interpreting_authored_html():
    body = "# RAG **evaluation**\n\nUse `recall@k` and *answer quality*.\nSoft line wraps\nstay together.\n\n## Checks\n\n- Retrieve evidence\n- Check the answer\n\n1. Read\n2. Verify\n\n| Signal | Meaning |\n| --- | --- |\n| `recall@k` | Retrieval |\n\n> A limitation.\n\n```python\nprint('<script>')\n```\n\n<script>alert(1)</script>\n[bad](http://localhost/private)"
    rendered = article_html(body, "Safe")
    for expected in ("<strong>evaluation</strong>", "<code>recall@k</code>", "<em>answer quality</em>", "<ul>", "<ol>", '<th scope="col">', "<blockquote>", "Soft line wraps stay together."):
        assert expected in rendered
    assert "<script>" not in rendered and 'href="http://localhost' not in rendered
    assert "&lt;script&gt;" in rendered and "default-src 'none'" in rendered


def test_plain_copy_preserves_code_operators_and_social_extracts_do_not_repeat_caveats():
    body = "# A **technical** explanation\n\n**Use** `recall@k` carefully. However, *the limitation* matters.\n\nThe expression `2 ** 3` means exponentiation.\n\nIllustrative, unexecuted Python:\n\n```python\n    value = 2 ** 3\n```\n"
    plain = plain_markdown(body)
    assert "    value = 2 ** 3" in plain
    assert "The expression 2 ** 3 means exponentiation." in plain
    assert "Use recall@k carefully" in plain and "*the limitation*" not in plain
    article = {"id": "revision-a", "markdown": body, "content_hash": markdown_hash(body)}
    data = build_publication(article, PublicationRequest(content_hash=article["content_hash"]), {})
    assert data["title"] == "A technical explanation"
    assert data["linkedin_text"].count("Use recall@k carefully.") == 1
    assert "Illustrative, unexecuted Python:" not in data["linkedin_text"]
    assert "However, the limitation matters." in data["linkedin_text"]
