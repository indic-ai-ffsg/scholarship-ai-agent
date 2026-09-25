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
