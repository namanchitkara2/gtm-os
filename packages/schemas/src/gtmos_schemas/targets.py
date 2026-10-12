"""What a run is looking for.

One shared core, two packs: an ICP describes accounts and the people inside
them; a RoleSpec describes candidates for an opening. They differ in fields and
in scoring rubric, and in nothing else — which is why every graph node between
``source`` and ``score`` is shared between sales and recruiting.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

TargetKind = Literal["icp", "role"]


class ICP(BaseModel):
    """Sales: the accounts, and the people in them, worth talking to."""

    titles: list[str] = Field(default_factory=list, description="Seniority/function to match")
    exclude_titles: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    headcount_min: int | None = None
    headcount_max: int | None = None
    funding_stages: list[str] = Field(default_factory=list)
    tech_stack: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    named_accounts: list[str] = Field(
        default_factory=list, description="Domains or company names to restrict to"
    )
    exclude_accounts: list[str] = Field(default_factory=list)


class RoleSpec(BaseModel):
    """Recruiting: the opening, and what a good candidate for it looks like."""

    role_title: str
    seniority: str | None = None
    must_have_skills: list[str] = Field(default_factory=list)
    nice_to_have_skills: list[str] = Field(default_factory=list)
    min_years_experience: int | None = None
    max_years_experience: int | None = None
    locations: list[str] = Field(default_factory=list)
    remote_ok: bool = True
    target_companies: list[str] = Field(
        default_factory=list, description="Where good candidates currently work"
    )
    exclude_companies: list[str] = Field(
        default_factory=list, description="Off-limits accounts, e.g. current clients"
    )
    industries: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)


class Target(BaseModel):
    """A named, reusable search definition owned by a tenant.

    Kept separate from the run so the same target can be re-run weekly and the
    cache can answer "have we already seen this person for this target".
    """

    id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    kind: TargetKind
    name: str
    brief: str = Field(description="The user's original free-text ask, kept verbatim")
    icp: ICP | None = None
    role: RoleSpec | None = None

    #: How the intake node read the brief, surfaced so the user can correct it
    #: rather than wonder why the results are wrong.
    interpretation_notes: list[str] = Field(default_factory=list)

    def spec(self) -> ICP | RoleSpec:
        spec = self.icp if self.kind == "icp" else self.role
        if spec is None:
            raise ValueError(f"target {self.id} is kind={self.kind} but carries no matching spec")
        return spec
