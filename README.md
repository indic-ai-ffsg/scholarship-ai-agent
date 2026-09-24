# Scholarship agent intelligence

Reads a scholarship page - or a block of pasted text - and returns one
structured record: dates, award amounts, eligibility, documents and the
application steps, with `null` wherever the source does not actually say.

The rule the whole service is built around:

> Never invent a value. If the source does not state it, return `null`.

The record is shaped for the Go API's `CuratedInput` rather than for what reads
nicely off a web page, which is what lets extracted eligibility become rules the
matcher evaluates instead of a paragraph nobody can match on.

## Setup

    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
    .venv/bin/playwright install chromium     # only needed for JS-rendered pages
    cp .env.example .env                      # then add your Gemini key

The key is `LLM_API_KEY`, not `GEMINI_API_KEY`. `.env.example` is the only list
of variables that is checked against the code; everything below repeats it.

## Two ways in

**The CLI**, one input at a time, printing JSON:

    python main.py https://example.org/scheme
    python main.py --refresh          # re-check every watched page, report changes
    python main.py --list             # what is being watched
    python main.py --unwatch <URL>    # stop watching a page
    python main.py --no-cache <URL>   # ignore Redis for this run

**The service**, which is what the admin panel drives:

    python server.py                  # http://127.0.0.1:8765
    python server.py --host 0.0.0.0 --port 9000

Host and port are command-line flags. There are no `DISCOVERY_HOST` or
`DISCOVERY_PORT` variables.

It serves JSON and no pages. The screen is in the admin panel under **AI
discovery**: paste URLs one per line (or a single block of raw text), press *Run
AI discovery*, and each source becomes a draft. A draft opens the panel's
ordinary authoring form with the boxes filled in, and nothing is saved until
somebody presses Save there.

    POST /api/runs              start a run over a list of sources
    GET  /api/runs/<id>/events  follow it - the agent's own log, as it happens
    POST /api/runs/<id>/cancel  stop after the source being read now
    GET  /api/schema            the record shape, from src/schema.py
    GET  /api/health            model, cache, schema version

Every route is also served under `DISCOVERY_PATH_PREFIX` (default `/discovery`),
which is stripped when present - so `/discovery/api/health` and `/api/health` are
the same route, and nginx can pass `$request_uri` through untouched.

## How it fits with the platform

Two services sit behind the admin panel. The Go API owns every row the platform
stores; this one owns reading a page, and owns no data at all - its Redis holds
extractions keyed by the hash of the page they came from, which can be thrown
away at any time and re-earned by reading the page again.

The panel reaches it at `/discovery` on its own origin, proxied exactly as the
API is (`DISCOVERY_TARGET` in admin/.env.example - that variable belongs to the
panel, not to this service), so there is no CORS anywhere.

- **The record is shaped like the listing it becomes.** `src/schema.py` is
  written against `CuratedInput` in the Go API rather than against what reads
  nicely off a web page: ISO dates, numeric amounts, `sponsor_type` as one of
  four values, `state_code` as 'BR' rather than 'Bihar', the RPwD conditions as
  `disability_type` stores them. That is what lets eligibility become rules the
  matcher evaluates instead of a paragraph nobody can match on.
- **Everything is checked again before it leaves.** `src/normalise.py` enforces
  the API's own limits and vocabularies, and reports every correction it made -
  the panel shows those beside the draft, because a value silently adjusted
  between the page and the form is the one thing an operator cannot review.
- **The run reports itself as it goes.** The lines under each item are the
  agent's own log: which page it read, which link it chose to follow, whether
  the record was reused because the source had not changed.
- **It binds to localhost on purpose.** It fetches whatever URL it is handed,
  which is not a thing to put on a network. A run is capped at 25 sources, and
  *Stop* takes effect after the source being read finishes. Where the panel that
  reaches it is public, set `DISCOVERY_VERIFY_URL` so that every run is checked
  against the API before anything is spent.

## How an extraction works

Three stages, and only the middle one is agentic - `src/agent.py` has the long
version, and the header comment in each module explains what it refuses to do:

1. **fetch** - deterministic. The cheapest tier that returns content: a plain
   request first, headless Chromium only if that comes back a shell.
2. **gather** - agentic. The model may follow links on the same site to fill a
   named gap, inside a budget (`SCHOLARSHIP_MAX_PAGES`, `_MAX_STEPS`,
   `_MAX_SECONDS`).
3. **extract** - deterministic. One schema-constrained call over everything read.

The gather stage is an LLM choosing **which pages to read**, not an LLM driving a
browser. It is handed the page's text and its links and calls `open_page(url)`;
it cannot click, type, scroll or work a dropdown. See "Not built yet" below.

    URL
     |
     +-- plain request -- enough text AND links to follow? --yes--> gather -> extract
     |                         |
     |                         no
     |                         |
     +-------------------> Chromium (src/render.py) -- still short? --> FetchError

Both halves of that test matter, and the second one was missing until
2026-09-24. A page can return plenty of text and no followable links - a
Next.js listing whose text comes from the SEO blob in `__NEXT_DATA__` while the
listing itself loads after hydration - and that is not a page that was read, it
is a dead end for the gather stage, which navigates by links. Rendering
buddy4study.com/scholarships turns 13,883 characters and 0 links into 43,140
and 60. The rendered read is kept only when it is actually better, so an
ordinary page that simply has no links falls back to the plain one.

Chrome tags are stripped with a guard for the same kind of reason. `<nav>`,
`<header>`, `<footer>`, `<aside>` and `<form>` are page furniture on most
sites, and an older government template will wrap its whole body in one:
depwd.gov.in/scholarships marks 2,978 of its 3,371 characters as `<header>`.
Stripping by tag name took it under the floor, so it rendered in Chromium for
six seconds and then failed. The text is now kept whenever stripping is the
difference between a usable page and a failed one.

A paste shorter than `NAME_ONLY_CHARS` skips all three and is **searched for**
instead, because it is a name rather than a source. Pasting a scheme's title and
nothing else used to extract from the title: every date, every amount and the
sponsor came back null, which is faithful and useless - or, where the model
recognised the name, came back filled in from what it remembered, which is worse
and looked identical. Either way the draft is weaker than one read from a page,
so it is marked `grounded` and the panel says so on it. A URL is still the better
input by some distance.

`NAME_ONLY_CHARS` is a constant in `src/agent.py` (400), not an environment
variable.

Caching is content-addressed, not time-boxed: the seed page is re-fetched every
run and hashed, so a cached record is reused only while the source is genuinely
unchanged. Redis is optional - without it, every run calls the model.

## What comes back

The field names are the schema's, not a web page's. `src/schema.py` is
canonical and `GET /api/schema` serves its JSON Schema; this is the shape, not
the whole list:

```json
{
  "name": "Example Scholarship",
  "sponsor": "Example Foundation",
  "sponsor_type": "NGO",
  "sponsor_type_text": "A registered charitable trust",
  "opens_at": null,
  "closes_at": "2027-03-31",
  "dates_text": "Applications open after the board results",
  "academic_year": "2026-27",
  "summary": "...",
  "description": "...",
  "award_basis": "NEED",
  "benefit_summary": "Up to Rs 50,000 a year",
  "award_amount_min": null,
  "award_amount_max": 50000,
  "currency": "INR",
  "who_qualifies": {
    "disability_percent_min": 40,
    "annual_family_income_max": 250000,
    "course_levels": ["SCHOOL"],
    "state_codes": ["BR"],
    "genders": null,
    "other_conditions": ["Must be studying at a recognised school"]
  },
  "documents_required": ["Income certificate", "Previous examination marksheet"],
  "application_process": {
    "steps": [{ "step_number": 1, "title": "Create an account", "description": "..." }],
    "application_links": [{ "label": "Apply on NSP", "url": "https://scholarships.gov.in" }]
  },
  "contacts": [{ "kind": "EMAIL", "value": "help@example.org", "label": "Scholarship cell" }]
}
```

Things worth knowing before writing anything against it:

- `sponsor_type` is one of `NGO`, `CORPORATE`, `GOVERNMENT`, `PRIVATE`. There is
  no "foundation" - a foundation is an `NGO` and `sponsor_type_text` carries the
  page's own words for it.
- The date fields are `opens_at` and `closes_at`, both ISO or null. There is no
  `deadline`. A date the page gives without a day ("April 2027") stays in
  `dates_text` and leaves `closes_at` null.
- The award is a range: `award_amount_min` / `award_amount_max`. "Up to Rs
  50,000" sets the maximum and leaves the minimum null.
- Eligibility is `who_qualifies`, a structured object. A restrictive list is null
  unless the scheme is actually restricted - a scheme open to any gender has
  `genders: null`, not all four values.

## Following a run

`GET /api/runs/<id>/events` streams JSON events. The `type` is one of:

    log            a line from the agent's own logger
    item_start     index
    item_skipped   index                       (the run was cancelled)
    item_done      index, record, seconds, report
    item_error     index, message, seconds
    fatal          message                     (the run itself failed)
    done           cancelled

There is no `{"status": "failed", "reason": "..."}` envelope and no vocabulary of
reason codes. A source that cannot be read emits `item_error` with the first line
of the exception in `message`, and the run carries on to the next source.

`report` on `item_done` carries `reused`, `grounded`, `pages_read`, `changes`,
`reworded` and `corrections`.

`GET /api/health` returns `ok`, `model`, `cache`, `max_items`, `schema_version`
and `reason` - `ok` is false with a `reason` when `LLM_API_KEY` is unset.

## Environment

Everything is optional except the key. The full list, with the defaults the code
actually uses, is in `.env.example`.

```env
LLM_API_KEY=                          # required

# MODEL=                              # required

# REDIS_URL=redis://localhost:6379/0  # or REDIS_HOST / REDIS_PORT

# SCHOLARSHIP_MAX_PAGES=5             # pages per extraction, including the seed
# SCHOLARSHIP_MAX_STEPS=8             # tool-calling turns
# SCHOLARSHIP_MAX_SECONDS=120         # wall clock for the gathering phase
# SCHOLARSHIP_ALLOWED_DOMAINS=        # extra hosts it may open
# SCHOLARSHIP_CLOSING_SOON_DAYS=7     # what --refresh calls "closing soon"

# DISCOVERY_VERIFY_URL=               # check the caller against the API first
# DISCOVERY_PATH_PREFIX=/discovery
# DISCOVERY_ALLOWED_ORIGINS=          # only when the panel is not proxying
```

## Security

It fetches arbitrary URLs supplied by its caller, so it must not be reachable
from the internet. It binds to 127.0.0.1 unless `--host` says otherwise.

    Internet -> Admin panel -> its own /discovery proxy -> this service

`DISCOVERY_VERIFY_URL` is what makes that safe when the panel is public: the
caller's `Authorization` header is handed to an ordinary read-only admin
endpoint, and 2xx means a platform user who may author listings. Unset, every run
is allowed - right for localhost, wrong the moment the panel is on the internet,
because anybody who can reach this can spend model credits with it.

## Layout

```text
main.py          CLI
server.py        HTTP API and event streaming
src/
  agent.py       the three stages, the cache decision, and the extract call
  tools.py       what the gathering agent may call, and its budgets
  fetcher.py     one page, by the cheapest tier that works
  scrapers.py    HTTP fetch, HTML to text, links and images
  render.py      tier 2: headless Chromium, pinned for reproducibility
  schema.py      the canonical record, written against CuratedInput
  normalise.py   API limits and vocabularies, and the corrections it reports
  cache.py       content-addressed Redis cache
  watch.py       --refresh over watched pages
```

Extraction lives in `agent.py`; there is no `extract.py` and no `models.py`.

## Design principles

1. **Grounded over complete.** `null` beats a guess. The one place this is not
   strictly true is a short paste, which is searched for and marked `grounded` so
   the draft says where it came from.
2. **Deterministic where possible.** Fetching, DOM extraction, hashing,
   validation and normalisation are ordinary code. The model is used for
   navigation and for the one extraction call, and nowhere else.
3. **The agent fills named gaps.** It is told what is missing and asked whether a
   link would supply it, not turned loose on a site.
4. **The platform schema is canonical**, so there is no translation layer between
   extraction and the matcher.
5. **Review before persistence.** Discovery makes a draft. A person presses Save.

## Not built yet

Named here because the architecture invites them and neither exists:

- **A browser agent.** The gather stage reads text and follows links; it cannot
  interact with a page. Portals whose content sits behind dropdowns, tabs or
  pagination with no distinct URL are the case it cannot read, and the fix is an
  interactive tier under `fetch_page`, not a change to gather.
- **Provenance.** Nothing records which page and which sentence a value came
  from, so "where did this come from?" is answered by re-reading the source. The
  run log is the nearest thing.
- **Tests.** There are none, and `pytest` is not a dependency. Verification today
  is running an extraction and reading the record.

## Non-goals

Not a general-purpose crawler, not a search engine, not a document store, not
the platform database, not an unrestricted browser, and not a replacement for
the operator who presses Save.
