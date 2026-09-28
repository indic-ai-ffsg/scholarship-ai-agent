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
