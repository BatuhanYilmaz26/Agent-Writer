"""Explicit model and research adapters; no implicit provider fallback."""

from fieldnote.providers.base import ModelGateway, ModelResult, ProviderError
from fieldnote.providers.fixture import FixtureGateway, FixtureResearchGateway
from fieldnote.providers.openrouter import OpenRouterGateway
from fieldnote.providers.tavily import TavilyResearchGateway
from fieldnote.providers.treg import TregResearchGateway

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
