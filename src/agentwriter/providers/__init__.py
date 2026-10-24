"""Explicit model and research adapters; no implicit provider fallback."""

from agentwriter.providers.base import ModelGateway, ModelResult, ProviderError
from agentwriter.providers.fixture import FixtureGateway, FixtureResearchGateway
from agentwriter.providers.openrouter import OpenRouterGateway
from agentwriter.providers.tavily import TavilyResearchGateway
from agentwriter.providers.treg import TregResearchGateway

__all__ = [
    "FixtureGateway",
    "FixtureResearchGateway",
    "ModelGateway",
    "ModelResult",
    "OpenRouterGateway",
    "ProviderError",
    "TavilyResearchGateway",
    "TregResearchGateway",
]
