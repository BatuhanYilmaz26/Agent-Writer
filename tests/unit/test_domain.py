"""Boundary regressions for real request and artifact contracts."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentwriter.domain import (
    ArticleRevision,
    ClaimCitation,
    ContentPlan,
    CreateRunRequest,
    EditorialReview,
    ResearchDecision,
    ReviewIssue,
    RunEvent,
    SectionDraft,
    SectionTask,
    Source,
    markdown_hash,
)


def task(task_id: str, order: int, words: int = 250) -> SectionTask:
    return SectionTask(id=task_id, order=order, title="A section", goal="Explain the idea", target_words=words)


def test_brief_bounds_unknown_fields_and_scalar_strictness() -> None:
    for change in (
        {"topic": "   "}, {"topic": "x" * 501}, {"target_words": "500"},
        {"target_words": True}, {"instructions": "x" * 4001},
        {"audience": "x" * 241}, {"tone": "x" * 121},
        {"language": "fr"}, {"owner_id": "another-user"},
        {"thread_id": "shared-thread"}, {"provider_base_url": "https://attacker.invalid"},
    ):
        with pytest.raises(ValidationError):
            CreateRunRequest.model_validate({"topic": "How forests grow", **change})


def test_request_hash_uses_normalized_full_brief() -> None:
    first = CreateRunRequest(topic="  How forests grow  ")
    same = CreateRunRequest.model_validate_json(first.model_dump_json())
    assert first.topic == "How forests grow"
    assert first.request_hash() == same.request_hash()
    assert first.request_hash() != CreateRunRequest(topic=first.topic, audience="Botanists").request_hash()


@pytest.mark.parametrize(
    "tasks", [
        (), (task("one", 1),),
        (task("same", 1), task("same", 2)),
        (task("one", 1), task("two", 1)),
        (task("one", 1), task("two", 3)),
        (task("one", 1, 260), task("two", 2)),
    ],
)
def test_invalid_plan_never_reaches_fanout(tasks: tuple[SectionTask, ...]) -> None:
    with pytest.raises(ValidationError):
        ContentPlan(title="Forests", target_words=500, tasks=tasks)


def test_valid_plan_json_roundtrip_and_immutable_order() -> None:
    plan = ContentPlan(title="Forests", target_words=500, tasks=(task("one", 1), task("two", 2)))
    restored = ContentPlan.model_validate_json(plan.model_dump_json())
    assert restored == plan
    assert plan.model_dump(mode="json")["tasks"][0]["id"] == "one"
    with pytest.raises(ValidationError):
        plan.title = "Changed"  # type: ignore[misc]


def test_decision_shape_cannot_bypass_effective_mode() -> None:
    with pytest.raises(ValidationError):
        ResearchDecision(need_research=False, effective_mode="required", reason="not consistent")
    with pytest.raises(ValidationError):
        ResearchDecision(need_research=False, effective_mode="off", reason="not allowed", queries=("a query",))


def test_source_dates_and_url_types_stay_distinct() -> None:
    base = dict(
        id="src_1", url="https://example.org/page", title="Evidence", provider="fixture",
        content_hash="a" * 64, retrieved_at=datetime.now(UTC),
    )
    assert Source(**base).model_dump(mode="json")["retrieved_at"].endswith("Z")
    for change in (
        {"url": "javascript:alert(1)"}, {"url": "https://user:secret@example.org"},
        {"retrieved_at": datetime(2026, 10, 24)}, {"content_hash": "not-a-hash"},
    ):
        with pytest.raises(ValidationError):
            Source.model_validate({**base, **change})


def test_review_approval_and_article_hash_are_independent_gates() -> None:
    issue = ReviewIssue(code="unsupported_claim", severity="error", explanation="No supporting excerpt")
    with pytest.raises(ValidationError):
        EditorialReview(approved=True, issues=(issue,))
    article = dict(run_id=uuid4(), title="Draft", content_markdown="# Draft\n\nText.", content_hash="a" * 64)
    with pytest.raises(ValidationError):
        ArticleRevision.model_validate(article)
    article["content_hash"] = markdown_hash(str(article["content_markdown"]))
    assert ArticleRevision.model_validate(article).review_status == "unassessed"
    with pytest.raises(ValidationError):
        ArticleRevision.model_validate({**article, "author_type": "human", "review_status": "approved"})


def test_markdown_bytes_are_not_trimmed_before_hash_or_download() -> None:
    markdown = "  # Draft\n\nText.\n"
    article = ArticleRevision(run_id=uuid4(), title="Draft", content_markdown=markdown, content_hash=markdown_hash(markdown))
    assert article.content_markdown == markdown
    assert ArticleRevision.model_validate_json(article.model_dump_json()).content_hash == markdown_hash(markdown)
    assert SectionDraft(task_id="one", title="Draft", content_markdown=markdown).content_markdown == markdown
    with pytest.raises(ValidationError):
        SectionDraft(task_id="one", title="Draft", content_markdown=" \n ")


def test_claim_support_is_bound_to_exact_excerpts() -> None:
    base = dict(
        article_revision_id=uuid4(), article_hash="a" * 64, claim_locator="paragraph:2",
        claim_text="A factual statement.", excerpt_ids=("ex_1",), excerpt_hashes=("b" * 64,),
    )
    claim = ClaimCitation(**base)
    assert claim.assessment == "unassessed"
    with pytest.raises(ValidationError):
        ClaimCitation.model_validate({**base, "excerpt_hashes": ()})
    with pytest.raises(ValidationError):
        ClaimCitation.model_validate({**base, "excerpt_ids": ("ex_1", "ex_1"), "excerpt_hashes": ("b" * 64, "b" * 64)})


def test_event_payload_rejects_non_json_nan_and_oversized_body() -> None:
    base = dict(run_id=uuid4(), sequence=1, event_type="queued")
    assert RunEvent(**base).model_dump(mode="json")["schema_version"] == 1
    for value in ({"raw": object()}, {"cost": float("nan")}, {"body": "x" * 17000}):
        with pytest.raises(ValidationError):
            RunEvent.model_validate({**base, "payload": value})
