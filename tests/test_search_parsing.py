"""Parsing a real LinkedIn search page, against a captured payload.

The fixture is a genuine `search_people` response. It is worth keeping precisely
because it contains the trap: fifteen person references for ten results, with
the extra five being mutual connections that LinkedIn names on the page.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gtmos_control.workflows import extract_result_people, extract_usernames, username_from_url

FIXTURE = Path(__file__).parent / "fixtures" / "search_people.json"


@pytest.fixture(scope="module")
def payload() -> dict:
    return json.loads(FIXTURE.read_text())


def test_mutual_connections_are_not_sourced(payload: dict) -> None:
    """The whole reason this parser reads the page text instead of the links."""
    people, _ = extract_result_people(payload)
    usernames = {u for u, _ in people}
    references = {r["text"] for r in payload["references"]["search_results"]}

    # These appear in `references` as ordinary person links but are named on the
    # page only as mutual connections of the real results.
    for bystander in ("rohandas", "kavya-rao-1a2b3c", "neilshah22",
                      "sameergill21", "amitverma30", "kabir-anand-1a2b3c",
                      "devtan", "maya-levi-1a2b3c"):
        assert bystander not in usernames, (
            f"{bystander} is a mutual connection, not a search result — sourcing it "
            "would spend a LinkedIn call scoring someone the ICP never matched"
        )
    assert len(references) > len(people), "fixture should contain more refs than results"


def test_finds_the_real_results_in_page_order(payload: dict) -> None:
    people, _ = extract_result_people(payload)
    names = [n for _, n in people]
    assert names[:4] == ["Priya Nair", "Arjun Mehra", "Daniel Stone", "Viktor Orlov"]
    assert all(u and not u.startswith("/") for u, _ in people)


def test_reports_results_it_could_not_reach(payload: dict) -> None:
    """Upstream returns fewer links than results; that must be visible, not silent."""
    people, unlinked = extract_result_people(payload)
    assert unlinked, "this fixture has results without links; they must be reported"
    assert "Sara Lopez" in unlinked
    assert not set(unlinked) & {n for _, n in people}, "a name cannot be both found and unlinked"


def test_limit_is_respected(payload: dict) -> None:
    assert len(extract_usernames(payload, limit=3)) == 3


@pytest.mark.parametrize("url,expected", [
    ("/in/priyanair/", "priyanair"),
    ("https://www.linkedin.com/in/williamhgates", "williamhgates"),
    ("/in/arjun-mehra-1a2b3c/", "arjun-mehra-1a2b3c"),
    ("/company/acme/", None),
    ("", None),
])
def test_username_extraction(url: str, expected: str | None) -> None:
    assert username_from_url(url) == expected


def test_empty_payload_is_not_a_crash() -> None:
    people, unlinked = extract_result_people({})
    assert people == [] and unlinked == []


# ---------------------------------------------------------- profile parsing --

PROFILES = json.loads((Path(__file__).parent / "fixtures" / "profile_texts.json").read_text())


@pytest.mark.parametrize("username,name,headline_contains,location", [
    ("priyanair", "Priya Nair", "Head of Growth at AcmeFulfill", "Delhi, India"),
    ("arjun-mehra-1a2b3c", "Arjun Mehra", "Head of Growth, Skyline Air",
     "Gurugram, Haryana, India"),
    ("daniel-stone", "Daniel Stone", "Head of Growth at BuildKit",
     "Tel Aviv District, Israel"),
])
def test_profile_fields_from_real_pages(username, name, headline_contains, location) -> None:
    from gtmos_control.workflows import parse_profile_text

    parsed = parse_profile_text(PROFILES[username])
    assert parsed["full_name"] == name
    assert headline_contains in (parsed["headline"] or "")
    assert parsed["location"] == location


def test_chrome_is_never_mistaken_for_a_headline() -> None:
    """The bug this filter exists for: '· 2nd' read as a job title.

    Every sourced person scored 0.0 because the connection degree was being
    stored as their headline, so no ICP term could ever match.
    """
    from gtmos_control.workflows import parse_profile_text

    for text in PROFILES.values():
        headline = parse_profile_text(text)["headline"] or ""
        assert "2nd" not in headline and "He/Him" not in headline, headline
        assert headline, "every one of these real profiles has a headline"


def test_profile_with_no_location_does_not_shift_fields() -> None:
    from gtmos_control.workflows import parse_profile_text

    parsed = parse_profile_text("Jane Roe\n\n· 2nd\n\nFounder at Acme\n\nContact info\n")
    assert parsed["full_name"] == "Jane Roe"
    assert parsed["headline"] == "Founder at Acme"
    assert parsed["location"] is None, "a missing location must stay missing, not borrow a line"
