"""The application, rather than a router model, controls research."""

import pytest
from pydantic import ValidationError

from agentwriter.domain import CreateRunRequest
from agentwriter.services.policy import determine_research


def test_explicit_required_cannot_be_downgraded_by_creative_brief() -> None:
    decision = determine_research(CreateRunRequest(topic="Write a poem about a forest", research_mode="required"))
    assert decision.need_research
    assert decision.reason == "explicit_required"
    assert decision.queries == ("Write a poem about a forest",)


@pytest.mark.parametrize("topic", [
    "Explain current energy prices", "Explain Python releases", "Explain sources for forest growth",
    "Explain the 2026 forest statistics", "Explain medical dosage", "Compare products with citations",
])
def test_required_facts_reject_research_off(topic: str) -> None:
    with pytest.raises(ValidationError, match="research_required"):
        CreateRunRequest(topic=topic, research_mode="off")
    decision = determine_research(CreateRunRequest(topic=topic, research_mode="auto"))
    assert decision.need_research
    assert decision.effective_mode == "required"


def test_instructions_cannot_smuggle_current_facts_into_off_mode() -> None:
    with pytest.raises(ValidationError):
        CreateRunRequest(topic="Write a poem about trees", instructions="Use current prices and cite sources", research_mode="off")


@pytest.mark.parametrize("topic", ["Write a short story about a dragon", "How forests grow"])
def test_clear_general_or_creative_drafts_can_disable_research(topic: str) -> None:
    decision = determine_research(CreateRunRequest(topic=topic, research_mode="off"))
    assert not decision.need_research
    assert not decision.queries
    assert decision.effective_mode == "off"
    assert "unresearched" in decision.reason


def test_ambiguous_auto_brief_remains_researched() -> None:
    decision = determine_research(CreateRunRequest(topic="Cloud markets", research_mode="auto"))
    assert decision.need_research
    assert decision.reason == "ambiguous_factual_brief"


def test_even_unvalidated_objects_cannot_bypass_off_policy() -> None:
    unsafe = CreateRunRequest.model_construct(topic="Latest prices", audience="Everyone", tone="clear", language="en", target_words=1200, research_mode="off", instructions="")
    with pytest.raises(ValueError, match="research_required"):
        determine_research(unsafe)
