from datetime import UTC, datetime, timedelta

from fieldnote.domain import CreateRunRequest
from fieldnote.services.technical import source_metadata, technical_issues


def codes(raw, **changes):
    request = CreateRunRequest(topic="A technical explanation", editorial_profile="technical-v1", **changes)
    return {issue.code for issue in technical_issues(raw, request, {})}


def test_code_and_personal_experience_require_context_without_executing_anything():
    assert "code_execution_unverified" in codes('```python\nraise RuntimeError("must not run")\n```\n')
    assert "code_execution_unverified" not in codes('Illustrative, unexecuted code:\n\n```python\n' + 'print("example")\n' * 100 + '```\n')
    assert "author_experience_unverified" in codes("I deployed this in production.")
    assert "author_experience_unverified" not in codes("I deployed this in production.", author_notes="I deployed this in production.")
    assert "author_experience_unverified" in codes("I deployed this in production.", author_notes="An unrelated observation.")


def test_recent_retrieval_and_update_never_establish_a_new_event():
    request = CreateRunRequest(topic="The latest development", editorial_profile="technical-v1")
    now = datetime.now(UTC)
    source = {"url": "https://arxiv.org/abs/2601.00001", "retrieved_at": now.isoformat(), "updated_at": now.isoformat(),
              "published_at": (now - timedelta(days=200)).isoformat()}
    assert "freshness_unverified" in {i.code for i in technical_issues("A current claim.", request, {"source": source})}
    source["published_at"] = (now - timedelta(days=1)).isoformat()
    assert not technical_issues("A current claim.", request, {"source": source})
    metadata = source_metadata({"source": source})["source"]
    assert metadata["event_at"] is None
    assert "preprint" in metadata["source_kind"]
    assert metadata["retrieved_at"] != metadata["published_at"]


def test_selected_source_failure_is_visible_and_legacy_profile_unchanged():
    assert "selected_source_unavailable" in codes("An explanation.", reference_urls=["https://example.org/source"])
    legacy = CreateRunRequest(topic="An explanation")
    assert technical_issues("I deployed this.\n```python\npass\n```", legacy, {}) == []
