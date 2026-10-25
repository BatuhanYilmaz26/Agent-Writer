"""Research decisions enforced independently of any model output."""

from agentwriter.domain import CreateRunRequest, ResearchDecision
from agentwriter.domain.research import factual_research_reason, is_general_or_creative


def determine_research(request: CreateRunRequest) -> ResearchDecision:
    """Return the effective research policy without an LLM network call."""
    context = request.research_context()
    explicit_reason = "selected_sources" if request.reference_urls else factual_research_reason(request.topic, context)
    if request.research_mode == "required":
        reason = "explicit_required"
        needed = True
    elif request.research_mode == "off":
        if explicit_reason or not is_general_or_creative(request.topic, context):
            # Defense for unvalidated/model_construct inputs; ordinary callers
            # already get this error at the request validation boundary.
            raise ValueError("research_required: incompatible research-off brief")
        reason = "explicit_off_unresearched_draft"
        needed = False
    elif explicit_reason:
        reason = explicit_reason
        needed = True
    elif is_general_or_creative(request.topic, context):
        reason = "general_or_creative_drafting"
        needed = False
    else:
        reason = "ambiguous_factual_brief"
        needed = True
    return ResearchDecision(
        need_research=needed,
        effective_mode="required" if needed else "off",
        reason=reason,
        queries=(request.topic,) if needed else (),
    )


decide_research = determine_research
