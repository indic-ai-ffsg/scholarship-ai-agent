"""Reading a search reply into candidates, without a network or a key.

The model is asked for a JSON array and does not always send one cleanly: it
fences it, pads it with a sentence, repeats a scheme under two spellings, or
offers an address it made up. Each of those is a row on the operator's screen
that either duplicates another or can only fail when read, so each is dropped
here rather than shown.
"""

import pytest

from src.search import DEFAULT_TOPIC, SearchError, _prompt, parse, resolve

GOOD = {
    "name": "SBI Platinum Jubilee Asha Scholarship",
    "sponsor": "SBI Foundation",
    "url": "https://www.sbiashascholarship.co.in/",
    "closes_at": "2026-10-31",
    "note": "For students from low-income families.",
}


def test_a_clean_array_is_read():
    import json
    [c] = parse(json.dumps([GOOD]))
    assert c["name"] == GOOD["name"]
    assert c["url"] == GOOD["url"]
    assert c["closes_at"] == "2026-10-31"


def test_a_fenced_reply_with_prose_around_it_is_read():
    import json
    text = "Here are the schemes I found:\n```json\n" + json.dumps([GOOD]) + "\n```\nHope that helps."
    assert len(parse(text)) == 1


def test_a_candidate_without_a_web_address_is_dropped():
    import json
    rows = [
        {**GOOD, "url": None},
        {**GOOD, "name": "Two", "url": "www.example.org/scheme"},
        {**GOOD, "name": "Three", "url": "mailto:someone@example.org"},
    ]
    assert parse(json.dumps(rows)) == []


def test_the_same_scheme_twice_is_one_row():
    import json
    rows = [
        GOOD,
        {**GOOD, "url": "https://www.sbiashascholarship.co.in"},       # same page, no slash
        {**GOOD, "name": "SBI platinum-jubilee ASHA scholarship",       # same name, other page
         "url": "https://example.org/sbi-asha"},
    ]
    assert len(parse(json.dumps(rows))) == 1


def test_a_closing_date_that_is_not_a_date_is_left_empty():
    import json
    [c] = parse(json.dumps([{**GOOD, "closes_at": "end of October"}]))
    assert c["closes_at"] is None


def test_a_reply_that_is_not_a_list_is_an_error():
    with pytest.raises(SearchError):
        parse("I could not find any scholarships matching that.")


def test_an_ordinary_address_is_not_followed():
    # Only search redirect links are resolved; anything else goes out untouched,
    # and without a request - this test has no network.
    assert resolve(GOOD["url"]) == GOOD["url"]


def test_the_prompt_refuses_portals_and_guessed_addresses():
    prompt = _prompt(DEFAULT_TOPIC, 10)
    assert "Never an entry for a portal" in prompt
    assert "Never construct or guess an address" in prompt


# --- vetting: the three failures of the first live run (2026-09-28) ----------

from datetime import date

from src.search import vet

TODAY = date(2026, 9, 29)


def _web(answers):
    """A stand-in for the web: address -> "ok" / "missing" / "unreadable"."""
    return lambda url: answers.get(url, "ok")


def test_a_scheme_that_has_closed_is_left_out():
    closed = {**GOOD, "name": "National Overseas Scholarship for ST Candidates",
              "url": "https://overseas.tribal.gov.in/scheme", "closes_at": "2026-07-31"}
    kept, dropped = vet([closed], [], TODAY, probe=_web({}))
    assert kept == []
    assert "closed on 31 July 2026" in dropped[0]


def test_a_scheme_with_no_closing_date_is_kept():
    # "Not stated" is not "closed".
    kept, _ = vet([{**GOOD, "closes_at": None}], [], TODAY, probe=_web({}))
    assert len(kept) == 1


def test_a_portal_home_page_is_left_out():
    rows = [
        {**GOOD, "name": "National Scholarship Portal Schemes", "url": "https://scholarships.gov.in/"},
        {**GOOD, "name": "National Overseas Scholarship", "url": "https://tribal.nic.in/"},
    ]
    kept, dropped = vet(rows, [], TODAY, probe=_web({}))
    assert kept == []
    assert len(dropped) == 2


def test_a_sponsors_own_single_scheme_site_is_not_a_portal():
    # sbiashascholarship.co.in/ is a bare home page AND the scheme.
    kept, _ = vet([GOOD], [], TODAY, probe=_web({}))
    assert kept[0]["url"] == GOOD["url"]


def test_an_invented_address_is_replaced_by_the_real_search_result():
    invented = {**GOOD, "name": "National Means-cum-Merit Scholarship Scheme (NMMSS)",
                "url": "https://www.education.gov.in/en/nmms", "closes_at": None}
    real = "https://scholarships.gov.in/public/schemeGuidelines/NMMSS.pdf"
    links = [("NMMSS - National Means-cum-Merit Scholarship guidelines", real)]
    kept, _ = vet([invented], links, TODAY, probe=_web({invented["url"]: "missing"}))
    assert kept[0]["url"] == real


def test_an_invented_address_with_no_real_result_is_left_out():
    invented = {**GOOD, "url": "https://www.education.gov.in/en/made-up"}
    kept, dropped = vet([invented], [], TODAY, probe=_web({invented["url"]: "missing"}))
    assert kept == []
    assert "does not exist" in dropped[0]


def test_a_page_that_cannot_be_read_is_offered_but_marked():
    # education.gov.in: a real address, an empty shell to a request and "Access
    # Denied" to a browser. Not a wrong address, so not dropped - but offered
    # unticked, because its draft could only come from a search.
    blocked = {**GOOD, "url": "https://www.education.gov.in/en/nmms"}
    kept, _ = vet([blocked], [], TODAY, probe=_web({blocked["url"]: "unreadable"}))
    assert kept[0]["readable"] is False


def test_an_unreadable_page_is_swapped_for_a_readable_result_about_the_same_scheme():
    blocked = {**GOOD, "name": "Central Sector Scheme of Scholarship for College and University Students",
               "url": "https://www.education.gov.in/en/central-sector-scheme-scholarship-college-and-university-students"}
    other = "https://example.org/central-sector-scheme-college-university-students-2026"
    unrelated = "https://example.org/pragati-scholarship"
    links = [("AICTE Pragati Scholarship", unrelated),
             ("Central Sector Scheme of Scholarship for College and University Students 2026", other)]
    kept, _ = vet([blocked], links, TODAY, probe=_web({blocked["url"]: "unreadable"}))
    assert kept[0]["url"] == other
    assert kept[0]["readable"] is True


def test_readable_candidates_come_first():
    a = {**GOOD, "name": "Blocked scheme", "url": "https://blocked.example.org/a"}
    b = {**GOOD, "name": "Open scheme", "url": "https://open.example.org/b"}
    kept, _ = vet([a, b], [], TODAY, probe=_web({a["url"]: "unreadable"}))
    assert [c["name"] for c in kept] == ["Open scheme", "Blocked scheme"]


# --- narration: what the panel shows while a search runs (2026-09-29) --------

def test_every_candidate_is_narrated_to_a_verdict():
    events = []
    rows = [
        {**GOOD, "name": "Open scheme", "url": "https://open.example.org/a"},
        {**GOOD, "name": "Blocked scheme", "url": "https://blocked.example.org/b"},
        {**GOOD, "name": "Old scheme", "url": "https://old.example.org/c", "closes_at": "2026-07-31"},
        {**GOOD, "name": "Portal Schemes", "url": "https://scholarships.gov.in/"},
    ]
    vet(rows, [], TODAY, probe=_web({"https://blocked.example.org/b": "unreadable"}),
        progress=events.append)

    last = {}
    for e in events:
        assert e["stage"] == "check" and e["message"]
        last[e["key"]] = e["status"]
    assert last == {
        "https://open.example.org/a": "readable",
        "https://blocked.example.org/b": "unreadable",
        "https://old.example.org/c": "closed",
        "https://scholarships.gov.in/": "portal",
    }
    # Each probed page was announced before it was judged.
    statuses = [e["status"] for e in events if e["key"] == "https://open.example.org/a"]
    assert statuses[0] == "checking"


def test_the_internal_key_does_not_leak_into_candidates():
    kept, _ = vet([GOOD], [], TODAY, probe=_web({}), progress=lambda e: None)
    assert "_key" not in kept[0]
