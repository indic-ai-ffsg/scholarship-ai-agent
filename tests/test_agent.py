"""Parsing the model's reply, and what a failed fetch is allowed to do."""

import pytest

from src import agent as agent_mod
from src.agent import ExtractionError, ScholarshipAgent, _grounded_prompt, _loads
from src.scrapers import FetchError

RECORD = '{"name": "A Scheme", "closes_at": "2027-03-31"}'


def test_a_bare_object_parses():
    assert _loads(RECORD)["name"] == "A Scheme"


def test_a_fenced_object_parses():
    assert _loads(f"```json\n{RECORD}\n```")["closes_at"] == "2027-03-31"


def test_an_object_padded_with_prose_parses():
    assert _loads(f"Here is the record you asked for:\n{RECORD}\nHope that helps.")["name"] == "A Scheme"


def test_a_reply_with_no_object_is_an_error():
    with pytest.raises(ExtractionError):
        _loads("I could not find that scholarship.")


def test_the_grounded_prompt_says_which_case_it_is_in():
    by_url = _grounded_prompt("https://sponsor.example/x", is_url=True)
    by_name = _grounded_prompt("Post Matric Scholarship", is_url=False)
    assert "https://sponsor.example/x" in by_url
    assert "Post Matric Scholarship" in by_name
    assert by_url != by_name


@pytest.fixture
def offline_agent(monkeypatch):
    """An agent with no key, no client and no cache - nothing here calls out."""
    monkeypatch.setenv("LLM_API_KEY", "not-a-real-key")
    monkeypatch.setattr(agent_mod.genai, "Client", lambda **kw: object())
    return ScholarshipAgent(use_cache=False)


def test_a_sweep_does_not_search_for_a_page_it_could_not_reach(offline_agent, monkeypatch):
    """ground_on_failure=False is what stops a re-check destroying a record.

    A watched URL whose host was down for ten minutes was searched for instead,
    and the empty record that came back overwrote the good one - award amount to
    null, name to "Unknown Scholarship". On a timer, that is data loss.
    """
    monkeypatch.setattr(agent_mod, "fetch_page", lambda url: (_ for _ in ()).throw(FetchError("host is down")))

    with pytest.raises(FetchError):
        offline_agent.run("https://sponsor.example/gone", ground_on_failure=False)


def test_a_first_extraction_still_falls_back_to_search(offline_agent, monkeypatch):
    """The same failure, when somebody has just asked for that page, is grounded
    rather than refused - a marked draft beats nothing."""
    monkeypatch.setattr(agent_mod, "fetch_page", lambda url: (_ for _ in ()).throw(FetchError("host is down")))
    monkeypatch.setattr(offline_agent, "_extract", lambda content, grounded: {"name": "Found by searching"})

    record, report = offline_agent.run("https://sponsor.example/gone")
    assert report.grounded is True
    assert record["name"] == "Found by searching"


def test_transient_model_errors_are_retried_and_permanent_ones_are_not():
    """A 503 cost a whole run once - the page had been fetched, rendered and
    read, and the extraction call was the only step left.

    The codes matter as much as the retrying. Asking again after a 400 produces
    the same bad request, and retrying a 404 just makes a wrong model name take
    four times as long to report.
    """
    from src.agent import RETRY

    for transient in (429, 500, 502, 503, 504):
        assert transient in RETRY.http_status_codes

    for permanent in (400, 401, 403, 404):
        assert permanent not in RETRY.http_status_codes

    assert RETRY.attempts > 1
    # Bounded: a source that is never going to succeed must not hold the run
    # open, and the queue behind it is somebody waiting.
    assert RETRY.max_delay <= 30
