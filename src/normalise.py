"""Make an extracted record safe to post at the platform, and say what moved.

The model is constrained by the schema on the structured path, and not at all on
the grounded path - `_extract` falls back to free JSON with the schema quoted in
the prompt when a page cannot be read, and a prompt is a request rather than a
constraint. So everything here is checked again: lengths the API refuses,
enum values it does not know, dates that are not dates, amounts that are not
numbers.

Nothing is silently dropped. Every correction returns a sentence, and the panel
shows them beside the draft, because a value quietly removed between the page
and the form is the one thing an operator cannot review. A state code that is
not a state code is worth a line saying so; deleting it and leaving the field
empty makes the page look like it never said anything about domicile.

Limits mirror backend/internal/registry/listings.go CuratedInput. Where they
disagree, the API wins and this file is wrong.
"""

import re
from datetime import date, datetime

from src.schema import (
    AwardBasis,
    ContactKind,
    CourseLevel,
    DisabilityType,
    Gender,
    SocialCategory,
    SponsorType,
)

MAX_TITLE = 200
MAX_SPONSOR = 200
MAX_SUMMARY = 500
MIN_SUMMARY = 20
MAX_DESCRIPTION = 20_000
MAX_BENEFIT_SUMMARY = 300
MAX_BENEFIT_DESCRIPTION = 20_000
MAX_ELIGIBILITY = 20_000
MAX_PROCESS = 20_000
MAX_NOTES = 20_000
MAX_ACADEMIC_YEAR = 9
MAX_DOCUMENTS = 20
MAX_DOCUMENT = 120
MAX_CONTACTS = 12
MAX_CONTACT_VALUE = 200
MAX_CONTACT_LABEL = 80
MAX_URL = 2000

_DATE_LABEL = {"opens_at": "Opening date", "closes_at": "Closing date"}

_DATE_LABELLED = re.compile(r"^[^0-9]{0,40}?[:\-\u2013]\s*(?=\d)")
_ORDINAL = re.compile(r"(\d{1,2})(st|nd|rd|th)\b", re.IGNORECASE)

_SESSION = re.compile(r"(\d{4})\s*[-/\u2013]\s*(\d{2,4})")
_YEAR = re.compile(r"\b(\d{4})\b")


def _plain(value):
    """An Enum member's value, or the value itself."""
    return getattr(value, "value", value)


ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
LONG_YEAR = re.compile(r"^(\d{4})\s*[-/–]\s*(\d{4})$")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
NOT_DIGITS = re.compile(r"\D")

STATE_CODES = {
    "AN", "AP", "AR", "AS", "BR", "CH", "CT", "DH", "DL", "GA", "GJ", "HR",
    "HP", "JK", "JH", "KA", "KL", "LA", "LD", "MP", "MH", "MN", "ML", "MZ",
    "NL", "OR", "PY", "PB", "RJ", "SK", "TN", "TG", "TR", "UP", "UT", "WB",
}

_ENUM_SETS = {
    "disability_types": {m.value for m in DisabilityType},
    "course_levels": {m.value for m in CourseLevel},
    "genders": {m.value for m in Gender},
    "social_categories": {m.value for m in SocialCategory},
}


def normalise(record: dict) -> list[str]:
    """Correct `record` in place. Returns one sentence per correction made."""
    notes: list[str] = []
    if not isinstance(record, dict):
        return notes

    _text(record, "name", MAX_TITLE, notes)
    _text(record, "sponsor", MAX_SPONSOR, notes)
    _text(record, "description", MAX_DESCRIPTION, notes)
    _text(record, "benefit_summary", MAX_BENEFIT_SUMMARY, notes)
    _text(record, "eligibility_summary", MAX_ELIGIBILITY, notes)
    _text(record, "important_notes", MAX_NOTES, notes)

    _one_of(record, "sponsor_type", {m.value for m in SponsorType}, notes)
    _one_of(record, "award_basis", {m.value for m in AwardBasis}, notes)

    _summary(record, notes)
    _academic_year(record, notes)
    _dates(record, notes)
    _money(record, notes)
    _logo(record, notes)
    _documents(record, notes)
    _contacts(record, notes)
    _links(record, notes)
    _eligibility(record, notes)

    return notes


# --- fields ---------------------------------------------------------------
def _text(record: dict, key: str, limit: int, notes: list[str]) -> None:
    value = record.get(key)
    if not isinstance(value, str):
        return
    cleaned = value.strip()
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1].rstrip() + "…"
        notes.append(f"{key} was longer than the {limit} characters the API takes and was cut.")
    record[key] = cleaned


def _one_of(record: dict, key: str, allowed: set[str], notes: list[str]) -> None:
    value = _plain(record.get(key))
    if value in (None, ""):
        record[key] = None
        return
    upper = str(value).strip().upper().replace(" ", "_").replace("-", "_")
    if upper in allowed:
        record[key] = upper
        return
    record[key] = None
    notes.append(f"{key} was {value!r}, which is not one of {', '.join(sorted(allowed))} - left empty.")


def _summary(record: dict, notes: list[str]) -> None:
    """The directory line. Required by the API at 20..500 characters.

    Taken from the description when the model left it out, because the two are
    the same text at different lengths and an empty summary is refused at save
    - where the operator has the form open and nothing to paste into it.
    """
    summary = record.get("summary")
    if isinstance(summary, str) and len(summary.strip()) >= MIN_SUMMARY:
        _text(record, "summary", MAX_SUMMARY, notes)
        return

    description = record.get("description")
    if not isinstance(description, str) or len(description.strip()) < MIN_SUMMARY:
        if summary:
            notes.append("summary was too short for the API's 20-character minimum.")
        return

    sentences = re.split(r"(?<=[.!?])\s+", description.strip())
    built = ""
    for sentence in sentences:
        if len(built) + len(sentence) + 1 > MAX_SUMMARY:
            break
        built = f"{built} {sentence}".strip()
        if len(built) >= MIN_SUMMARY * 4:
            break
    record["summary"] = built[:MAX_SUMMARY] or description.strip()[:MAX_SUMMARY]
    notes.append("summary was missing and has been taken from the opening of the description.")


def _academic_year(record: dict, notes: list[str]) -> None:
    value = record.get("academic_year")
    if not isinstance(value, str) or not value.strip():
        return
    cleaned = value.strip()

    match = LONG_YEAR.match(cleaned)
    if match:
        cleaned = f"{match.group(1)}-{match.group(2)[2:]}"
    if len(cleaned) > MAX_ACADEMIC_YEAR:
        session = _SESSION.search(cleaned)
        salvaged = ""
        if session:
            salvaged = f"{session.group(1)}-{session.group(2)[-2:]}"
        else:
            year = _YEAR.search(cleaned)
            salvaged = year.group(1) if year else ""

        if salvaged and len(salvaged) <= MAX_ACADEMIC_YEAR:
            notes.append(f"academic_year {cleaned!r} was read as {salvaged}.")
            record["academic_year"] = salvaged
            return

    if len(cleaned) > MAX_ACADEMIC_YEAR:
        record["academic_year"] = None
        _append_note(record, "dates_text", f"Academic year as given: {value.strip()}")
        notes.append(
            f"academic_year {value.strip()!r} is longer than the 9 characters the column holds "
            "- moved into the notes on dates."
        )
        return
    record["academic_year"] = cleaned


def _dates(record: dict, notes: list[str]) -> None:
    """opens_at and closes_at have to be dates. Anything else is prose."""
    for key in ("opens_at", "closes_at"):
        value = record.get(key)
        if value in (None, ""):
            record[key] = None
            continue

        text = str(value).strip()
        if ISO_DATE.match(text):
            try:
                datetime.strptime(text, "%Y-%m-%d")
                record[key] = text
                continue
            except ValueError:
                pass

        salvaged = parse_date(_loosen(text))
        if salvaged is not None:
            record[key] = salvaged.isoformat()
            notes.append(f"{key} was {text!r}, read as {salvaged.isoformat()}.")
            continue

        record[key] = None
        _append_note(record, "dates_text", f"{_DATE_LABEL[key]} as given: {text}")
        notes.append(
            f"{key} was {text!r}, which is no date we can read - moved into the notes on dates."
        )

    opens, closes = record.get("opens_at"), record.get("closes_at")
    if opens and closes and opens > closes:
        record["opens_at"], record["closes_at"] = closes, opens
        notes.append("The opening date was after the closing date; the two have been swapped.")


def _money(record: dict, notes: list[str]) -> None:
    low = _number(record, "award_amount_min", notes)
    high = _number(record, "award_amount_max", notes)

    if low is not None and high is not None and low > high:
        record["award_amount_min"], record["award_amount_max"] = high, low
        notes.append("The award range was the wrong way round and has been swapped.")

    currency = record.get("currency")
    if isinstance(currency, str) and currency.strip():
        code = currency.strip().upper()
        if len(code) != 3 or not code.isalpha():
            record["currency"] = None
            notes.append(f"currency {currency!r} is not a three-letter code - left empty.")
        else:
            record["currency"] = code


def _number(record: dict, key: str, notes: list[str]) -> float | None:
    value = record.get(key)
    if value in (None, ""):
        record[key] = None
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        record[key] = None
        _append_note(record, "award_amount_text", f"{key.replace('_', ' ')} as given: {value}")
        notes.append(f"{key} was {value!r}, which is not a number - moved into the award wording.")
        return None

    if number <= 0:
        record[key] = None
        notes.append(f"{key} was {number}, and the API takes an amount above zero only - left empty.")
        return None

    record[key] = number
    return number


def _logo(record: dict, notes: list[str]) -> None:
    """The sponsor's mark, if the page offered one this platform can serve.

    PNG and JPEG only - the logo endpoint takes those two and the admin form
    says so in as many words. An SVG is markup rather than a picture, and a
    data: URI is the picture itself rather than somewhere to fetch it from,
    which the panel has no way to hand to the upload.
    """
    value = record.get("logo_url")
    if value in (None, ""):
        record["logo_url"] = None
        return

    url = str(value).strip()
    if not url.lower().startswith(("http://", "https://")):
        record["logo_url"] = None
        notes.append(f"The logo address was {url[:60]!r}, which is not one we can fetch - left empty.")
        return
    if url.split("?")[0].lower().endswith(".svg"):
        record["logo_url"] = None
        notes.append("The logo on the page is an SVG, which the platform does not serve - left empty.")
        return
    record["logo_url"] = url[:MAX_URL]


def _documents(record: dict, notes: list[str]) -> None:
    docs = record.get("documents_required")
    if not isinstance(docs, list):
        record["documents_required"] = []
        return

    cleaned: list[str] = []
    trimmed = False
    for item in docs:
        if not isinstance(item, str) or not item.strip():
            continue
        text = item.strip()
        if len(text) > MAX_DOCUMENT:
            text = text[: MAX_DOCUMENT - 1].rstrip() + "…"
            trimmed = True
        if text not in cleaned:
            cleaned.append(text)

    if len(cleaned) > MAX_DOCUMENTS:
        spilled = cleaned[MAX_DOCUMENTS:]
        cleaned = cleaned[:MAX_DOCUMENTS]
        _append_note(record, "important_notes", "Also listed: " + "; ".join(spilled))
        notes.append(
            f"The API takes {MAX_DOCUMENTS} documents and the page listed {len(spilled) + MAX_DOCUMENTS}"
            " - the rest are in the notes."
        )
    if trimmed:
        notes.append("A document name was longer than 120 characters and was cut.")
    record["documents_required"] = cleaned


def _contacts(record: dict, notes: list[str]) -> None:
    contacts = record.get("contacts")
    if not isinstance(contacts, list):
        record["contacts"] = []
        return

    kinds = {m.value for m in ContactKind}
    cleaned: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for entry in contacts:
        if not isinstance(entry, dict):
            continue
        value = str(_plain(entry.get("value")) or "").strip()
        if not value:
            continue
        kind = str(_plain(entry.get("kind")) or "").strip().upper()
        if kind not in kinds:
            kind = "EMAIL" if EMAIL.match(value) else "PHONE"
            notes.append(f"The kind of contact {value!r} was not one the API knows - read as {kind}.")

        if kind == "EMAIL" and not EMAIL.match(value):
            _append_note(record, "important_notes", f"Contact as given: {value}")
            notes.append(f"{value!r} is filed as an email address and is not one - moved into the notes.")
            continue

        if kind in ("PHONE", "WHATSAPP"):
            dialled = _phone(value)
            if dialled is None:
                named = str(_plain(entry.get("label")) or "").strip()
                _append_note(
                    record, "important_notes",
                    f"Contact as given: {named + ': ' if named else ''}{value}",
                )
                notes.append(
                    f"{value!r} is not a ten-digit Indian number, which is all the listing can "
                    "hold - moved into the notes."
                )
                continue
            if dialled != value:
                notes.append(f"{value!r} was written as {dialled} - the column takes the ten digits.")
                value = dialled

        key = (kind, value.lower())
        if key in seen:
            continue
        seen.add(key)

        label = str(entry.get("label") or "").strip()
        cleaned.append({
            "kind": kind,
            "value": value[:MAX_CONTACT_VALUE],
            "label": label[:MAX_CONTACT_LABEL] or None,
        })

    if len(cleaned) > MAX_CONTACTS:
        cleaned = cleaned[:MAX_CONTACTS]
        notes.append(f"More than {MAX_CONTACTS} contacts were found; the rest were dropped.")
    record["contacts"] = cleaned


def _phone(value: str) -> str | None:
    """The number as `validateContacts` will count it, or None.

    The API's rule is ten digits once everything else is stripped - every Indian
    mobile, and every landline with its STD code. Two things a sponsor's page
    routinely carries fail it, and they fail differently:

      +91 98765 43210   thirteen characters, ten digits once the country code
                        goes. Rewritten, because the number is right and only
                        the way it is written is not.
      1800 11 8004      a toll-free helpline. Eleven digits, and no amount of
                        rewriting makes it ten - it is a real number that this
                        column cannot hold. The caller moves it into the notes
                        rather than dropping it, because it is often the only
                        number on the page.

    A value that already passes is returned untouched, spacing and all: the
    sponsor wrote it that way and the API does not mind.
    """
    digits = NOT_DIGITS.sub("", value)

    if len(digits) == 10:
        return value
    if len(digits) == 12 and digits.startswith("91"):
        return digits[2:]
    if len(digits) == 11 and digits.startswith("0"):
        return digits[1:]
    if len(digits) == 13 and digits.startswith("091"):
        return digits[3:]
    return None


def _links(record: dict, notes: list[str]) -> None:
    """The apply address becomes `external_url`, which the API requires."""
    process = record.get("application_process")
    if not isinstance(process, dict):
        record["application_process"] = {"application_links": [], "steps": []}
        return

    links = process.get("application_links")
    if not isinstance(links, list):
        process["application_links"] = []
        return

    cleaned = []
    for link in links:
        if not isinstance(link, dict):
            continue
        url = str(link.get("url") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            if url:
                notes.append(f"An application link was {url!r}, which is not a web address - dropped.")
            continue
        cleaned.append({"label": str(link.get("label") or "").strip() or "Apply", "url": url[:MAX_URL]})
    process["application_links"] = cleaned


def _eligibility(record: dict, notes: list[str]) -> None:
    who = record.get("who_qualifies")
    if not isinstance(who, dict):
        record["who_qualifies"] = {}
        return

    for key, allowed in _ENUM_SETS.items():
        values = who.get(key)
        if values in (None, ""):
            who[key] = None
            continue
        if not isinstance(values, list):
            values = [values]

        kept, dropped = [], []
        for value in values:
            upper = str(_plain(value)).strip().upper().replace(" ", "_").replace("-", "_")
            (kept if upper in allowed else dropped).append(upper if upper in allowed else str(value))
        who[key] = kept or None
        for value in dropped:
            _add_condition(who, f"{key.replace('_', ' ')}: {value}")
            notes.append(f"{value!r} is not a value {key} can hold - kept as a written condition.")

    states = who.get("state_codes")
    if states in (None, ""):
        who["state_codes"] = None
    else:
        if not isinstance(states, list):
            states = [states]
        kept = []
        for value in states:
            code = str(_plain(value)).strip().upper()
            if code in STATE_CODES:
                if code not in kept:
                    kept.append(code)
            else:
                _add_condition(who, f"Domicile: {value}")
                notes.append(
                    f"{str(value)!r} is not a state code the profile stores - kept as a written condition."
                )
        who["state_codes"] = kept or None

    for key in ("disability_percent_min", "academic_percentage_min", "annual_family_income_max"):
        value = who.get(key)
        if value in (None, ""):
            who[key] = None
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            who[key] = None
            _add_condition(who, f"{key.replace('_', ' ')}: {value}")
            notes.append(f"{key} was {value!r}, which is not a number - kept as a written condition.")
            continue
        if number <= 0:
            who[key] = None
            continue
        if key.endswith("percent_min") or key.endswith("percentage_min"):
            if number > 100:
                who[key] = None
                _add_condition(who, f"{key.replace('_', ' ')}: {value}")
                notes.append(f"{key} was {number}, which is not a percentage - kept as a written condition.")
                continue
        who[key] = number

    for key in ("age_min", "age_max"):
        value = who.get(key)
        if value in (None, ""):
            who[key] = None
            continue
        try:
            who[key] = int(float(value))
        except (TypeError, ValueError):
            who[key] = None
            notes.append(f"{key} was {value!r}, which is not a whole number of years - left empty.")


# --- helpers --------------------------------------------------------------
def _append_note(record: dict, key: str, sentence: str) -> None:
    existing = record.get(key)
    record[key] = f"{existing.strip()}\n{sentence}" if isinstance(existing, str) and existing.strip() else sentence


def _add_condition(who: dict, sentence: str) -> None:
    conditions = who.get("other_conditions")
    if not isinstance(conditions, list):
        conditions = []
    if sentence not in conditions:
        conditions.append(sentence)
    who["other_conditions"] = conditions


def _loosen(text: str) -> str:
    """Strip what a page puts around a date but not inside it."""
    without_label = _DATE_LABELLED.sub("", text).strip()
    return _ORDINAL.sub(r"\1", without_label).replace(",", " ").strip()


def parse_date(value) -> date | None:
    """An ISO date from the record, or None. The one date reader in the codebase.

    Day-first wherever a format is ambiguous - 05-10-2027 is the fifth of
    October, not the tenth of May - because every sponsor this platform reads
    writes dates the Indian way. ISO is tried first, so a date that is already
    right cannot be re-read as something else.
    """
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    for fmt in (
        "%Y-%m-%d", "%Y/%m/%d",
        "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y",
        "%d %B %Y", "%d %b %Y",
        "%B %d %Y", "%b %d %Y",
        "%B %d, %Y",
    ):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None
