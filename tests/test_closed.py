"""A draft is never made from a scheme that has already closed, or from a
portal's home page - including when the record comes out of the cache, which
is how both reached the screen on 2026-09-28."""

from datetime import date, timedelta

import pytest

from src.agent import CatalogueSource, ClosedScheme, _is_portal_home, _refuse_if_closed


def test_a_closed_scheme_is_refused():
    past = (date.today() - timedelta(days=1)).isoformat()
    with pytest.raises(ClosedScheme):
        _refuse_if_closed({"name": "Old scheme", "closes_at": past})


def test_an_open_or_undated_scheme_is_not():
    future = (date.today() + timedelta(days=30)).isoformat()
    _refuse_if_closed({"name": "Open", "closes_at": future})
    _refuse_if_closed({"name": "Undated", "closes_at": None})


@pytest.mark.parametrize("url", ["https://scholarships.gov.in/", "https://tribal.nic.in"])
def test_a_government_home_page_is_a_portal(url):
    assert _is_portal_home(url)


def test_a_scheme_page_on_a_government_site_is_not():
    assert not _is_portal_home(
        "https://www.education.gov.in/en/central-sector-scheme-scholarship-college-and-university-students")


def test_catalogue_source_is_what_a_portal_raises():
    # The run loop reports any RuntimeError as a failed item; both are one.
    assert issubclass(CatalogueSource, RuntimeError)
    assert issubclass(ClosedScheme, RuntimeError)
