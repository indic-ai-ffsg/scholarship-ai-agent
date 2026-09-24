"""The timer's default, which is off."""

from src.sweeper import ENV_INTERVAL, interval_hours

def test_unset_means_no_sweeping(monkeypatch):
    monkeypatch.delenv(ENV_INTERVAL, raising=False)
    assert interval_hours() == 0.0


def test_zero_means_no_sweeping(monkeypatch):
    monkeypatch.setenv(ENV_INTERVAL, "0")
    assert interval_hours() == 0.0


def test_an_unparseable_value_is_off_rather_than_a_crash(monkeypatch):
    monkeypatch.setenv(ENV_INTERVAL, "nightly")
    assert interval_hours() == 0.0


def test_a_number_turns_it_on(monkeypatch):
    monkeypatch.setenv(ENV_INTERVAL, "24")
    assert interval_hours() == 24.0
