"""The brief parser, and the failure that made it necessary.

The first test in the Ourshopee section is the one that matters. On 26 Sep 2026 a
real call to ``define_target`` with a brief naming one company returned
``interpreted_as: {}``, ``start_run`` accepted it, the source node fell back to a
cached ``"head of growth"`` keyword, and six LinkedIn actions — out of a hard
~100/day ceiling — bought seven people unrelated to the ask. Every assertion
about refusal in this file traces to that run.
"""

from __future__ import annotations

import pytest
from gtmos_graphs.brief import PARSER_VERSION, BriefInterpretation, parse_brief

# ------------------------------------------------- the regression that matters --

class TestOurshopeeRegression:
    """A brief the old code turned into an empty spec and then executed."""

    BRIEF = (
        "Find the product and growth leaders at Ourshopee, the Dubai-based "
        "e-commerce marketplace. I want to talk to whoever owns the storefront "
        "and category experience."
    )

    def test_brief_is_no_longer_interpreted_as_empty(self):
        result = parse_brief(self.BRIEF)
        assert result.icp.model_dump(exclude_defaults=True) != {}, (
            "THE ORIGINAL BUG: this exact brief produced interpreted_as: {} and "
            "was then executed against a live LinkedIn budget"
        )

    def test_the_named_company_is_extracted(self):
        result = parse_brief(self.BRIEF)
        assert "Ourshopee" in result.icp.named_accounts

    def test_the_functions_are_extracted(self):
        result = parse_brief(self.BRIEF)
        joined = " ".join(result.icp.titles).lower()
        assert "product" in joined and "growth" in joined

    def test_dubai_is_extracted_as_a_location(self):
        result = parse_brief(self.BRIEF)
        assert "Dubai" in result.icp.locations

    def test_it_is_executable_so_a_real_ask_is_not_blocked(self):
        result = parse_brief(self.BRIEF)
        assert result.executable, (
            f"a well-formed brief must run; confidence={result.confidence}, "
            f"fields={result.discriminating_fields}"
        )
        assert result.refusal() is None

    def test_an_unreadable_brief_refuses_instead_of_spending_actions(self):
        """The other half of the fix: no criteria means no run."""
        result = parse_brief("Find me some good people to talk to next quarter.")
        assert not result.executable
        refusal = result.refusal()
        assert refusal is not None
        assert refusal["error"] == "brief_not_understood"
        assert "needed" in refusal and "how_to_fix" in refusal, (
            "a refusal a model cannot act on sends the client into a retry loop"
        )

    def test_a_cached_keyword_can_never_substitute_for_a_parse(self):
        """An empty parse must not inherit criteria from anywhere else.

        The old failure was not only that parsing returned nothing — it was that
        something downstream supplied 'head of growth' anyway. An empty ICP must
        stay empty and be refused, not be quietly filled.
        """
        result = parse_brief("aaaaaa bbbbbb cccccc")
        assert result.icp.titles == []
        assert result.icp.keywords == []
        assert not result.executable


# --------------------------------------------------------- realistic briefs --

@pytest.mark.parametrize(
    "brief,expect_title_contains,expect_locations",
    [
        (
            "Find VP Marketing at Series B/C SaaS companies in the US with 100-500 employees.",
            "marketing", ["United States"],
        ),
        (
            "Heads of Growth at Indian fintech startups",
            "growth", ["India"],
        ),
        (
            "I need CTOs in Bangalore at Series A companies",
            "cto", ["Bangalore"],
        ),
        (
            "Directors of RevOps in London, B2B SaaS, over 200 employees",
            "revops", ["London"],
        ),
        (
            "founders of seed stage marketplaces in Singapore",
            "founder", ["Singapore"],
        ),
    ],
)
def test_realistic_briefs_parse_and_execute(brief, expect_title_contains, expect_locations):
    result = parse_brief(brief)
    assert result.executable, f"{brief!r} -> conf {result.confidence}"
    joined = " ".join(result.icp.titles).lower()
    assert expect_title_contains in joined, f"{brief!r} -> titles {result.icp.titles}"
    for loc in expect_locations:
        assert loc in result.icp.locations, f"{brief!r} -> locations {result.icp.locations}"


def test_the_spec_from_the_user_request_parses_fully():
    """The exact example given in the production-hardening brief."""
    result = parse_brief(
        "Find VP Marketing at Series B/C SaaS companies in the US with 100-500 employees."
    )
    icp = result.icp
    assert any("marketing" in t.lower() for t in icp.titles)
    assert "SaaS" in icp.industries
    assert "Series B" in icp.funding_stages and "Series C" in icp.funding_stages
    assert icp.headcount_min == 100 and icp.headcount_max == 500
    assert "United States" in icp.locations


# ------------------------------------------------------------ normalization --

class TestDeterministicNormalization:
    def test_place_aliases_are_canonicalised(self):
        """Bengaluru and Bangalore must produce the same stored spec.

        Otherwise two users asking the same thing get different cache keys, and a
        target re-run weekly changes meaning when someone edits its wording.
        """
        a = parse_brief("heads of growth in Bengaluru")
        b = parse_brief("heads of growth in Bangalore")
        assert a.icp.locations == b.icp.locations == ["Bangalore"]

    def test_gurgaon_normalises_to_gurugram(self):
        assert "Gurugram" in parse_brief("PMs in Gurgaon").icp.locations

    def test_parsing_is_deterministic(self):
        brief = "VP Sales at Series C fintech companies in Mumbai, 200-800 employees"
        first = parse_brief(brief)
        for _ in range(5):
            again = parse_brief(brief)
            assert again.icp.model_dump() == first.icp.model_dump()
            assert again.confidence == first.confidence

    def test_headcount_given_backwards_is_repaired_and_noted(self):
        result = parse_brief("marketing leaders at companies with 500-100 employees")
        assert result.icp.headcount_min == 100
        assert result.icp.headcount_max == 500
        assert any("high-to-low" in n for n in result.notes)

    def test_plus_suffix_headcount(self):
        assert parse_brief("CMOs at 1000+ employee retailers").icp.headcount_min == 1000

    def test_at_least_n_employees_is_not_read_as_a_company_name(self):
        """'at least 100 employees' must not become named_accounts=['least'].

        This is the class of bug that silently restricts a search to a company
        that does not exist, returning nothing and burning the actions anyway.
        """
        result = parse_brief("Find VP Marketing at least 100 employees in the US")
        assert not any("least" in a.lower() for a in result.icp.named_accounts)

    def test_series_b_companies_is_not_read_as_a_company_name(self):
        result = parse_brief("Find VPs at Series B companies in India")
        assert not any("series" in a.lower() for a in result.icp.named_accounts)
        assert "Series B" in result.icp.funding_stages


# ------------------------------------------------------- execution gating --

class TestExecutionGate:
    def test_headcount_alone_is_not_executable(self):
        """LinkedIn people-search cannot filter on headcount.

        A spec carrying only a headcount bound would pass a naive "is the ICP
        empty" check and then search the entire platform.
        """
        result = parse_brief("companies with 100-500 employees")
        assert result.icp.headcount_min == 100, "the bound is still parsed"
        assert not result.executable, "but it cannot drive a search on its own"

    def test_funding_stage_alone_is_not_executable(self):
        result = parse_brief("Series B and Series C")
        assert result.icp.funding_stages
        assert not result.executable

    def test_location_alone_is_not_executable(self):
        """'People in Dubai' is not a search, it is a census."""
        result = parse_brief("people in Dubai")
        assert "Dubai" in result.icp.locations
        assert not result.executable

    def test_empty_brief_is_refused_not_crashed(self):
        result = parse_brief("")
        assert not result.executable
        assert result.confidence == 0.0
        assert result.refusal()["error"] == "brief_not_understood"

    def test_none_brief_is_refused_not_crashed(self):
        assert not parse_brief(None).executable  # type: ignore[arg-type]

    def test_long_brief_that_parsed_poorly_is_penalised(self):
        """The Ourshopee shape: many words, few readable criteria."""
        wordy = (
            "I would really like to speak with some interesting individuals who "
            "might potentially be open to having a conversation about the things "
            "we are building, ideally somebody fairly senior who cares about the "
            "space and has opinions about where it is all going next year or so."
        )
        result = parse_brief(wordy)
        assert not result.executable, (
            "a long brief the parser mostly failed to read is less trustworthy "
            "than a short one, not more"
        )

    def test_confidence_floor_is_enforced_independently_of_fields(self):
        """A spec can hold a discriminating field and still be refused."""
        result = BriefInterpretation(
            icp=parse_brief("VP Marketing in the US").icp, confidence=0.10,
        )
        assert result.discriminating_fields, "it has titles"
        assert not result.executable, "but confidence is below the floor"
        assert result.refusal()["error"] == "brief_confidence_too_low"


# ------------------------------------------------------------------- hints --

class TestCallerHints:
    def test_caller_hints_override_the_parse_per_field(self):
        result = parse_brief(
            "growth people in India", hints={"titles": ["Head of Growth Marketing"]}
        )
        assert result.icp.titles == ["Head of Growth Marketing"]
        assert result.provenance["titles"] == "supplied_by_caller"

    def test_hints_do_not_erase_fields_they_omit(self):
        """A client passing only titles must not lose the location in the brief."""
        result = parse_brief("VP Sales in Singapore", hints={"titles": ["VP Sales"]})
        assert "Singapore" in result.icp.locations
        assert result.provenance["locations"] == "parsed_from_brief"

    def test_empty_hints_are_ignored_rather_than_overwriting(self):
        """The original bug in one line: empty client structure must not win."""
        result = parse_brief(
            "Heads of Growth at Indian fintech startups",
            hints={"titles": [], "locations": None, "industries": []},
        )
        assert result.icp.titles, "empty hints must not blank a good parse"
        assert result.executable

    def test_unknown_hint_keys_are_ignored_not_crashed(self):
        result = parse_brief("VP Marketing in the US", hints={"not_a_field": "x"})
        assert result.executable


def test_parser_version_is_reported():
    """A result set must be attributable to the parser that produced it."""
    assert parse_brief("VP Marketing in the US").parser_version == PARSER_VERSION


def test_ambiguity_is_raised_when_companies_named_without_roles():
    result = parse_brief("Find people at Ourshopee")
    assert any("no role" in a.lower() for a in result.ambiguities)
