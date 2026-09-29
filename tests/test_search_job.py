"""A narrated search, end to end through the job and its event stream, with the
model and the web stood in for. What the panel reads: progress events in order,
then search_done with the candidates, then done."""

import json
import time

import server
from src import search


class _Response:
    def __init__(self, text):
        self.text = text
        self.candidates = []


class _Models:
    def generate_content(self, **_):
        return _Response(json.dumps([
            {"name": "Open scheme", "url": "https://open.example.org/a", "closes_at": None},
            {"name": "Old scheme", "url": "https://old.example.org/b", "closes_at": "2020-01-01"},
        ]))


class _Agent:
    def __init__(self, **_):
        self.client = type("C", (), {"models": _Models()})()
        self.model_name = "stand-in"


def test_a_search_job_streams_progress_then_its_result(monkeypatch):
    monkeypatch.setattr(server, "ScholarshipAgent", _Agent)
    monkeypatch.setattr(search, "_probe", lambda url: "ok")
    monkeypatch.setattr(search, "resolve", lambda url: url)

    job = server.start_search_job("anything", 5)
    for _ in range(100):
        if job.closed:
            break
        time.sleep(0.02)
    kinds = [e["type"] for e in job.history]

    assert kinds[0] == "progress" and kinds[-1] == "done"
    assert "search_done" in kinds
    done = next(e for e in job.history if e["type"] == "search_done")
    assert [c["name"] for c in done["candidates"]] == ["Open scheme"]
    assert any("closed" in why for why in done["left_out"])
    checks = [e for e in job.history if e["type"] == "progress" and e.get("stage") == "check"]
    assert {e["status"] for e in checks} >= {"checking", "readable", "closed"}
