"""Chromium's launch flags: one unconditional, one deliberately not."""

from src.render import BASE_ARGS, _launch_args


def test_dev_shm_is_always_disabled(monkeypatch):
    monkeypatch.delenv("CHROMIUM_NO_SANDBOX", raising=False)
    assert "--disable-dev-shm-usage" in _launch_args()


def test_the_sandbox_stays_on_unless_asked(monkeypatch):
    monkeypatch.delenv("CHROMIUM_NO_SANDBOX", raising=False)
    assert _launch_args() == list(BASE_ARGS)
    assert "--no-sandbox" not in _launch_args()


def test_the_escape_hatch_works_when_a_runtime_needs_it(monkeypatch):
    monkeypatch.setenv("CHROMIUM_NO_SANDBOX", "1")
    assert "--no-sandbox" in _launch_args()
