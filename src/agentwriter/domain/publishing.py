"""Exact-revision publication inputs; these never authorize external posting."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator

from agentwriter.domain.models import DomainModel, Hash, _utc
from agentwriter.services.urls import validate_public_url

Platform = Literal["medium", "linkedin", "x"]


class PublicationRequest(DomainModel):
    content_hash: Hash
    canonical_url: str | None = None
    linkedin_text: Annotated[str, StringConstraints(strict=True, max_length=3000)] | None = None
    x_posts: Annotated[tuple[Annotated[str, StringConstraints(strict=True, max_length=5000)], ...], Field(min_length=1, max_length=5)] | None = None
    thread_length: Literal[1, 3, 4, 5] = 1
    disclosure: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=500)] | None = None
    search_title: Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=300)] | None = None
    search_description: Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=500)] | None = None
    facts_checked: bool = False
    voice_checked: bool = False
    language_checked: bool = False
    variants_checked: bool = False
    disclosure_checked: bool = False

    @field_validator("canonical_url")
    @classmethod
    def safe_url(cls, value: str | None) -> str | None:
        return validate_public_url(value) if value else None

    @field_validator("linkedin_text", "disclosure", "search_title", "search_description")
    @classmethod
    def valid_text(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or any(0xD800 <= ord(c) <= 0xDFFF for c in value)):
            raise ValueError("Use nonblank Unicode text")
        return value

    @field_validator("search_title", "search_description")
    @classmethod
    def single_line_metadata(cls, value: str | None) -> str | None:
        if value and ("\n" in value or "\r" in value):
            raise ValueError("Use a single line for search metadata")
        return value

    @field_validator("x_posts")
    @classmethod
    def valid_posts(cls, posts: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if posts and any(not p.strip() or any(0xD800 <= ord(c) <= 0xDFFF for c in p) for p in posts):
            raise ValueError("Use nonblank Unicode posts")
        return posts


class PublicationRecordRequest(DomainModel):
    platform: Platform
    url: str
    published_at: datetime

    @field_validator("url")
    @classmethod
    def safe_url(cls, value: str) -> str:
        return validate_public_url(value)

    @field_validator("published_at")
    @classmethod
    def utc_date(cls, value: datetime) -> datetime:
        return _utc(value)
