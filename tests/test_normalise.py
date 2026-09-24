"""normalise() corrects a record and reports what it changed.

The reporting is the half that matters: a value silently adjusted between the
page and the form is the one thing an operator cannot review.
"""

from src.normalise import normalise, parse_date


def test_a_landline_keeps_its_std_code_as_ten_digits():
    record = {"contacts": [{"kind": "PHONE", "value": "0120-6619540", "label": "helpline"}]}
    notes = normalise(record)
    assert record["contacts"][0]["value"] == "1206619540"
    assert any("1206619540" in n for n in notes)


def test_a_toll_free_number_moves_to_the_notes_rather_than_being_dropped():
    record = {"contacts": [{"kind": "PHONE", "value": "1800 180 8004", "label": "helpline"}]}
    notes = normalise(record)
    assert record["contacts"] == []
    assert any("notes" in n for n in notes)
    assert "1800 180 8004" in (record.get("important_notes") or "")


def test_a_number_that_already_passes_is_left_alone():
    record = {"contacts": [{"kind": "PHONE", "value": "9876543210", "label": None}]}
    notes = normalise(record)
    assert record["contacts"][0]["value"] == "9876543210"
    assert notes == []


def test_normalise_is_idempotent():
    record = {"contacts": [{"kind": "PHONE", "value": "0120-6619540", "label": "helpline"}]}
    normalise(record)
    assert normalise(record) == []


def test_parse_date_reads_what_the_schema_promises():
    assert parse_date("2027-03-31") is not None
    assert parse_date(None) is None
    assert parse_date("not a date") is None
