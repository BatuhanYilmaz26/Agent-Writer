"""Fail-closed boundary until the selected Treg account contract is qualified."""

from typing import Any

from agentwriter.providers.base import ProviderError


class TregResearchGateway:
    """Public documentation alone cannot authorize a guessed paid waterfall.

    RES-01/02 require account-specific schemas, billing controls and fixtures.
    Keep this explicit refusal wired rather than silently switching vendors.
    """

    async def search(self, query: str, max_results: int = 5) -> list[dict[str, str]]:
        del query, max_results
        raise ProviderError("research_provider_not_qualified")

    async def extract(self, url: str) -> dict[str, Any]:
        del url
        raise ProviderError("research_provider_not_qualified")
