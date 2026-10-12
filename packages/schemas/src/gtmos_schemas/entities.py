"""The people and companies the platform actually sells.

These are the types the upstream LinkedIn MCP cannot give you. Its tools return
``{url, sections: {name -> raw text}}`` and tell the caller's LLM to parse it
(``tools/person.py``). Parsing into *these* models, server-side and once, is what
makes a database, a cache, and a dedupe key possible at all.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from gtmos_schemas.evidence import Sourced

#: How confident we are that a contact channel will actually reach the person.
#: ``verified`` is reserved for a real SMTP/HLR check, never for a provider
#: simply asserting it — providers assert constantly and are wrong often.
ContactStatus = Literal["verified", "probable", "guessed", "invalid", "unknown"]


class PersonIdentity(BaseModel):
    """The stable part of a person: what makes two records the same person.

    ``linkedin_urn`` is the only globally stable id LinkedIn exposes; usernames
    are user-editable and change. Dedupe on urn first, then on
    (normalised name, current company domain), and never on email — one person
    has many, and one shared inbox has many people.
    """

    linkedin_urn: str | None = None
    linkedin_username: str | None = None
    full_name: str
    headline: str | None = None
    location: str | None = None

    @property
    def dedupe_key(self) -> str:
        if self.linkedin_urn:
            return f"urn:{self.linkedin_urn}"
        if self.linkedin_username:
            return f"li:{self.linkedin_username.lower()}"
        return f"name:{self.full_name.strip().lower()}"


class Employment(BaseModel):
    company_name: str
    company_domain: str | None = None
    company_linkedin_urn: str | None = None
    title: str
    started_on: date | None = None
    ended_on: date | None = None
    is_current: bool = False
    location: str | None = None
    description: str | None = None


class Education(BaseModel):
    school: str
    degree: str | None = None
    field_of_study: str | None = None
    started_on: date | None = None
    ended_on: date | None = None


class Person(BaseModel):
    """A sourced, enriched person. Every contact channel carries its evidence."""

    model_config = ConfigDict(populate_by_name=True)

    identity: PersonIdentity

    emails: list[Sourced[str]] = Field(default_factory=list)
    phones: list[Sourced[str]] = Field(default_factory=list)
    email_status: ContactStatus = "unknown"
    phone_status: ContactStatus = "unknown"

    employments: list[Employment] = Field(default_factory=list)
    educations: list[Education] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    summary: Sourced[str] | None = None

    #: Free-form signals the scoring node reads: recent job change, hiring
    #: activity, tech mentions, conference talks. Deliberately untyped — the
    #: useful ones change faster than a schema migration can keep up.
    signals: dict[str, object] = Field(default_factory=dict)

    #: 0-100, produced by the scoring node against a Target. Not a data field:
    #: it is only meaningful relative to the target it was scored for.
    score: float | None = None
    score_rationale: str | None = None

    @property
    def current_employment(self) -> Employment | None:
        for e in self.employments:
            if e.is_current:
                return e
        return self.employments[0] if self.employments else None

    @property
    def best_email(self) -> Sourced[str] | None:
        return max(self.emails, key=lambda s: s.confidence, default=None)

    @property
    def best_phone(self) -> Sourced[str] | None:
        return max(self.phones, key=lambda s: s.confidence, default=None)

    @property
    def is_reachable(self) -> bool:
        return bool(self.emails or self.phones)


class Company(BaseModel):
    name: str
    domain: str | None = None
    linkedin_urn: str | None = None
    linkedin_username: str | None = None
    description: str | None = None
    industry: str | None = None
    headcount: int | None = None
    headcount_range: str | None = None
    hq_location: str | None = None
    founded_year: int | None = None
    funding_stage: str | None = None
    total_funding_usd: int | None = None
    tech_stack: list[str] = Field(default_factory=list)
    signals: dict[str, object] = Field(default_factory=dict)
    score: float | None = None

    @property
    def dedupe_key(self) -> str:
        if self.linkedin_urn:
            return f"urn:{self.linkedin_urn}"
        if self.domain:
            return f"domain:{self.domain.lower().removeprefix('www.')}"
        return f"name:{self.name.strip().lower()}"
