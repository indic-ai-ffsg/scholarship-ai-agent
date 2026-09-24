"""The tier gate: when a plain fetch is enough, and when it is not.

No network. fetch_html and render_html are replaced, so what is under test is
the decision, not the fetching.
"""

import pytest

from src import fetcher
from src.scrapers import FetchError

PROSE = "This scholarship is open to students in Class 9 and 10. " * 20


def page(visible="", island="", links=()):
    anchors = "".join(f'<a href="{u}">{t}</a>' for t, u in links)
    script = f'<script type="application/json">{island}</script>' if island else ""
    return f"<html><body>{script}<main><p>{visible}</p></main>{anchors}</body></html>"


@pytest.fixture
def no_network(monkeypatch):
    """Records whether the renderer was reached, and with what to answer."""
    state = {"rendered": 0, "plain": None, "render": None}

    def fake_fetch(url, timeout=None):
        if state["plain"] is None:
            raise FetchError("nothing to serve")
        return state["plain"]

    def fake_render(url, timeout=None):
        state["rendered"] += 1
        if state["render"] is None:
            raise FetchError("nothing to render")
        return state["render"]

    monkeypatch.setattr(fetcher, "fetch_html", fake_fetch)
    monkeypatch.setattr("src.render.render_html", fake_render)
    return state


def test_a_good_plain_page_is_not_rendered(no_network):
    no_network["plain"] = page(visible=PROSE, links=[("Eligibility", "/eligibility")])
    got = fetcher.fetch_page("https://sponsor.example/scheme")
    assert got.tier == "fetch"
    assert no_network["rendered"] == 0


def test_a_fat_island_over_an_empty_dom_still_renders(no_network):
    no_network["plain"] = page(visible="Loading", island='{"applicationDeadline": "2026-07-31"}' * 40,
                               links=[("Sign in", "/auth")])
    no_network["render"] = page(visible=PROSE, links=[("Apply", "/apply")])
    got = fetcher.fetch_page("https://sponsor.example/")
    assert no_network["rendered"] == 1
    assert got.tier == "render"
    assert "Class 9 and 10" in got.text


def test_plenty_of_text_and_nothing_to_follow_renders(no_network):
    no_network["plain"] = page(visible=PROSE, links=[])
    no_network["render"] = page(visible=PROSE, links=[("A scheme", "/s/1")])
    got = fetcher.fetch_page("https://sponsor.example/list")
    assert no_network["rendered"] == 1
    assert got.tier == "render"
    assert got.links


def test_the_render_has_to_earn_the_swap(no_network):
    no_network["plain"] = page(visible=PROSE, links=[])
    no_network["render"] = page(visible="", links=[])
    got = fetcher.fetch_page("https://sponsor.example/x")
    assert no_network["rendered"] == 1
    assert got.tier == "fetch"
    assert "Class 9 and 10" in got.text


def test_a_page_that_yields_nothing_raises(no_network):
    no_network["plain"] = page(visible="tiny", links=[])
    no_network["render"] = page(visible="tiny", links=[])
    with pytest.raises(FetchError):
        fetcher.fetch_page("https://sponsor.example/empty")
