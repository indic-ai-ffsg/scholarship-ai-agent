"""Telling a page that lists scholarships from a page that describes one.

The threshold is not delicate and the measurements are why: one scheme on a
sponsor's own page, one on a department's umbrella index, twenty-one on the NSP
catalogue. Nothing real was found in between.
"""

import pytest

from src.agent import CATALOGUE_MIN_SCHEMES, CatalogueSource
from src.scrapers import scheme_names

ONE_SCHEME = """
The SBI Platinum Jubilee Asha Scholarship is open now.
How do I apply for the SBI Platinum Jubilee Asha Scholarship?
Key milestones for the SBI Platinum Jubilee Asha Scholarship are below.
"""

A_CATALOGUE = "\n".join(
    f"{n} Scholarship Scheme Open from : 01-06-2026 Open till : 31-10-2026"
    for n in ("AICTE Pragati", "AICTE Saksham", "AICTE Swanath", "PM USP Special",
              "Central Sector Merit", "National Means Cum Merit", "Top Class Education")
)


def test_one_scheme_named_many_times_is_still_one():
    # The tail is the name; the head is whatever sentence carried it.
    assert len(scheme_names(ONE_SCHEME)) == 1


def test_a_catalogue_is_counted_as_many():
    assert len(scheme_names(A_CATALOGUE)) >= CATALOGUE_MIN_SCHEMES


def test_a_single_scheme_page_stays_under_the_threshold():
    assert len(scheme_names(ONE_SCHEME)) < CATALOGUE_MIN_SCHEMES


def test_fragments_with_unbalanced_brackets_are_not_names():
    text = "AICTE - Swanath Scholarship ( Technical Degree) (Welfare Based Scheme)"
    for name in scheme_names(text):
        assert name.count("(") == name.count(")"), name


def test_the_refusal_names_what_is_on_the_page(monkeypatch):
    """A refusal an operator can act on beats a draft they cannot check.

    Asked for one record, the NSP catalogue produced name "Schemes On NSP",
    sponsor "National Scholarship Portal", and a closing date taken from
    whichever scheme came first. Nothing in it looked wrong and no scholarship
    it described existed.
    """
    import os
    from src import agent as agent_mod
    from src.fetcher import Page

    monkeypatch.setenv("LLM_API_KEY", "not-a-real-key")
    monkeypatch.setattr(agent_mod.genai, "Client", lambda **kw: object())
    monkeypatch.setattr(agent_mod, "fetch_page",
                        lambda url: Page(url=url, text=A_CATALOGUE, links=[], tier="fetch"))

    agent = agent_mod.ScholarshipAgent(use_cache=False)
    with pytest.raises(CatalogueSource) as caught:
        agent.run("https://portal.example/all-scholarships")

    message = str(caught.value)
    assert "separate sources" in message
    assert "AICTE Pragati" in message


# --- the grounded half ------------------------------------------------------
#
# A URL that cannot be fetched is searched for instead, so there is no page to
# count scheme names in. The record is the only evidence, and it turns out to be
# enough: asked about a portal, the model says "portal".

from src.agent import _PORTAL_MARKERS_NEEDED, _portal_markers

ODISHA = {
    "name": "Odisha State Scholarship Schemes",
    "summary": "The Odisha State Scholarship Portal is an integrated single-window platform "
               "offering multiple state government scholarship schemes",
    "description": "an integrated online platform managed by the Government of Odisha that "
                   "brings together scholarship schemes offered by various state departments",
    "dates_text": "Different scholarship schemes hosted on the portal have individual timelines",
    "opens_at": None, "closes_at": None,
}

UMBRELLA = {
    "name": "Central Sector Umbrella Scheme Scholarships for Students with Disabilities",
    "summary": "Government scholarships and fellowship schemes administered by DEPwD",
    "description": "includes various programmes such as the National Fellowship for PwDs, "
                   "National Overseas Scholarship Scheme, Free Coaching",
    "dates_text": None, "opens_at": None, "closes_at": None,
}

REAL_SCHEME = {
    "name": "SBI Platinum Jubilee Asha Scholarship 2026-27",
    "summary": "financial aid between Rs 15,000 and Rs 15,00,000",
    "description": "one of India's largest scholarship initiatives",
    "dates_text": "New applications closed on 19 Sept 2026",
    "opens_at": "2026-07-22", "closes_at": "2026-09-19",
}


def refused(record):
    no_window = not record.get("opens_at") and not record.get("closes_at")
    return no_window and len(_portal_markers(record)) >= _PORTAL_MARKERS_NEEDED


def test_a_portal_that_states_no_dates_is_refused():
    assert refused(ODISHA)


def test_an_umbrella_scheme_is_not_a_portal():
    """"Central Sector Umbrella Scheme" is a real named scheme that happens to
    contain sub-schemes. It says "includes various programmes", never "portal",
    and DEPwD's page for it should still produce a listing."""
    assert _portal_markers(UMBRELLA) == []
    assert not refused(UMBRELLA)


def test_a_real_scheme_with_a_window_is_never_refused():
    assert not refused(REAL_SCHEME)


def test_portal_language_alone_is_not_enough():
    """A scheme applied for THROUGH a portal still has its own dates, and the
    dates are what tell the two apart."""
    through_a_portal = dict(REAL_SCHEME, summary="Apply on the national portal, "
                                                "a single-window platform for all schemes")
    assert len(_portal_markers(through_a_portal)) >= _PORTAL_MARKERS_NEEDED
    assert not refused(through_a_portal)
