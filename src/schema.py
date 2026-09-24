"""The record an extraction produces, shaped like the listing it becomes.

This file is the contract with the platform, not a convenient shape for reading
a web page, and the difference cost real work to discover. The first version of
it extracted what a sponsor's page says in the sponsor's own words — dates as
"15 April 2027", an award as "Rs 60,000 per year plus a one-time allowance", a
sponsor "type" of whatever noun the page used — and every one of those is
refused by `POST /admin/scholarships`:

    opens_at/closes_at   time.Time        an ISO date, not a phrase
    award_amount*        float64, gt=0    a number, not a sentence
    sponsor_type         oneof NGO CORPORATE GOVERNMENT PRIVATE
    award_basis          oneof MERIT NEED MERIT_CUM_MEANS CATEGORY OTHER
    academic_year        max=9            "2026-27", not "the 2026-27 session"
    summary              min=20,max=500
    benefit_summary      max=300
    contacts[].kind      oneof EMAIL PHONE WHATSAPP

So the model is asked for both: the platform's value AND, where the wording
carries something the value cannot, the sponsor's own words alongside it. That
is not the model inventing - it is reading a stated date and writing it as a
date. Where the page does not state something, the answer is null, and null is
always better than a plausible guess.

The eligibility block is the part that earns the most. `who_qualifies` used to
be prose - "40% or above", "Bihar, Jharkhand and West Bengal" - and prose is
unmatchable: the engine evaluates `eligibility_rule` rows keyed on
`profile_field`, so a percentage in a sentence reaches no student. Every field
here is named after the profile field it becomes and carries the exact stored
value, because a rule written as `state_code IN ("Bihar")` is accepted, stored,
and then silently matches nobody - the column holds 'BR'. That failure is
invisible from every direction, which is why the vocabularies are enumerated
here rather than described.

Mirrors, and must not drift from:
  backend/internal/registry/listings.go   CuratedInput
  backend/internal/domain/enums.go        OrgType, CourseLevel
  backend/.../0004_document_vault.sql     profile_field
  admin/src/pages/scholarship-vocabulary.ts  the same values, for the form
"""

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field

SCHEMA_VERSION = 3


class SponsorType(str, Enum):
    """`CuratedInput.SponsorType`, which is `domain.OrgType`."""

    NGO = "NGO"
    CORPORATE = "CORPORATE"
    GOVERNMENT = "GOVERNMENT"
    PRIVATE = "PRIVATE"


class AwardBasis(str, Enum):
    MERIT = "MERIT"
    NEED = "NEED"
    MERIT_CUM_MEANS = "MERIT_CUM_MEANS"
    CATEGORY = "CATEGORY"
    OTHER = "OTHER"


class CourseLevel(str, Enum):
    """`domain.CourseLevel`. Diploma and ITI are UNDERGRADUATE on a profile."""

    SCHOOL = "SCHOOL"
    UNDERGRADUATE = "UNDERGRADUATE"
    POSTGRADUATE = "POSTGRADUATE"
    DOCTORAL = "DOCTORAL"


class Gender(str, Enum):
    MALE = "MALE"
    FEMALE = "FEMALE"
    TRANSGENDER = "TRANSGENDER"
    UNDISCLOSED = "UNDISCLOSED"


class SocialCategory(str, Enum):
    GENERAL = "GENERAL"
    EWS = "EWS"
    OBC = "OBC"
    SC = "SC"
    ST = "ST"


class DisabilityType(str, Enum):
    """The 21 conditions the RPwD Act recognises, as `disability_type` stores them."""

    BLINDNESS = "BLINDNESS"
    LOW_VISION = "LOW_VISION"
    LEPROSY_CURED = "LEPROSY_CURED"
    HEARING_IMPAIRMENT = "HEARING_IMPAIRMENT"
    LOCOMOTOR_DISABILITY = "LOCOMOTOR_DISABILITY"
    DWARFISM = "DWARFISM"
    INTELLECTUAL_DISABILITY = "INTELLECTUAL_DISABILITY"
    MENTAL_ILLNESS = "MENTAL_ILLNESS"
    AUTISM_SPECTRUM_DISORDER = "AUTISM_SPECTRUM_DISORDER"
    CEREBRAL_PALSY = "CEREBRAL_PALSY"
    MUSCULAR_DYSTROPHY = "MUSCULAR_DYSTROPHY"
    CHRONIC_NEUROLOGICAL_CONDITION = "CHRONIC_NEUROLOGICAL_CONDITION"
    SPECIFIC_LEARNING_DISABILITY = "SPECIFIC_LEARNING_DISABILITY"
    MULTIPLE_SCLEROSIS = "MULTIPLE_SCLEROSIS"
    SPEECH_AND_LANGUAGE_DISABILITY = "SPEECH_AND_LANGUAGE_DISABILITY"
    THALASSEMIA = "THALASSEMIA"
    HAEMOPHILIA = "HAEMOPHILIA"
    SICKLE_CELL_DISEASE = "SICKLE_CELL_DISEASE"
    MULTIPLE_DISABILITIES = "MULTIPLE_DISABILITIES"
    ACID_ATTACK_VICTIM = "ACID_ATTACK_VICTIM"
    PARKINSONS_DISEASE = "PARKINSONS_DISEASE"


class ContactKind(str, Enum):
    EMAIL = "EMAIL"
    PHONE = "PHONE"
    WHATSAPP = "WHATSAPP"


class Contact(BaseModel):
    """One way to reach the sponsor (backend 0055, `registry.Contact`).

    Extracted rather than left in the notes because it used to land there: a
    page ending "Queries: access@trust.org" produced a record whose only contact
    was a sentence inside `important_notes`, and the listing reached students
    with no way to ask anybody anything.
    """

    kind: ContactKind = Field(description="EMAIL, PHONE or WHATSAPP")
    value: str = Field(description="The address or number exactly as the page gives it")
    label: Optional[str] = Field(
        None, description="Whose address it is: 'Helpline', 'Disability cell'. Null if unlabelled"
    )


class ApplicationStep(BaseModel):
    step_number: int = Field(description="Sequential step index starting at 1")
    title: str = Field(description="Action title for the step")
    subtitle: Optional[str] = Field(None, description="Additional context or prerequisites")
    description: str = Field(description="Detailed instructions for completing this step")


class ApplicationLink(BaseModel):
    label: str = Field(description="Name or description of the link destination")
    url: str = Field(description="Full web URL for applying")


class ApplicationProcess(BaseModel):
    application_links: List[ApplicationLink] = Field(
        default_factory=list,
        description="URLs where candidates submit applications, the real one first",
    )
    steps: List[ApplicationStep] = Field(
        default_factory=list, description="Ordered list of application steps"
    )


class EligibilityCriteria(BaseModel):
    """The conditions, in the values the matcher compares against.

    Every field below is named for the `profile_field` it becomes a rule on.
    Anything a scheme requires that is not one of these - an essay, a
    recommendation, an interview - belongs in `eligibility_summary` as prose,
    where it is honestly presented as something a person has to check.
    """

    disability_percent_min: Optional[float] = Field(
        None, description="Minimum certified disability percentage, as a number. 40 for '40% or above'"
    )
    disability_types: Optional[List[DisabilityType]] = Field(
        None,
        description="Only if the scheme is restricted to named conditions. Null means open to all",
    )
    academic_percentage_min: Optional[float] = Field(
        None, description="Minimum qualifying marks as a percentage number. Convert a CGPA only if the page gives the percentage too"
    )
    annual_family_income_max: Optional[float] = Field(
        None, description="Family income ceiling in rupees, as a number: 600000 for 'Rs 6,00,000'"
    )
    course_levels: Optional[List[CourseLevel]] = Field(
        None, description="Levels of study the scheme accepts. Diploma and ITI count as UNDERGRADUATE"
    )
    state_codes: Optional[List[str]] = Field(
        None,
        description="Two-letter state/UT codes of eligible domicile (BR, JH, WB, DL, TN...). Null if open to all of India",
    )
    social_categories: Optional[List[SocialCategory]] = Field(
        None, description="Only if restricted by category. Null means no category restriction"
    )
    genders: Optional[List[Gender]] = Field(
        None, description="Only if restricted. Null means open to everyone - do not list all four"
    )
    age_min: Optional[int] = Field(None, description="Minimum age in years, if stated")
    age_max: Optional[int] = Field(None, description="Maximum age in years, if stated")

    qualification_text: Optional[str] = Field(
        None, description="The course or qualification wording as the page gives it"
    )
    entrance_exam_accepted: Optional[str] = Field(
        None, description="Required or accepted entrance exams, as stated"
    )
    other_conditions: Optional[List[str]] = Field(
        None,
        description="Stated conditions no field above can hold, one per entry",
    )


class ScholarshipSchema(BaseModel):
    name: str = Field(description="Full official name of the scholarship")
    sponsor: str = Field(description="Organisation or entity offering the award")
    sponsor_type: Optional[SponsorType] = Field(
        None, description="NGO, CORPORATE, GOVERNMENT or PRIVATE. Null if the page does not make it clear"
    )
    sponsor_type_text: Optional[str] = Field(
        None, description="How the page describes the sponsor, if it does: 'CSR trust', 'autonomous body'"
    )

    opens_at: Optional[str] = Field(
        None, description="Application start date as YYYY-MM-DD. Null if not stated"
    )
    closes_at: Optional[str] = Field(
        None, description="Application deadline as YYYY-MM-DD. Null if not stated"
    )
    dates_text: Optional[str] = Field(
        None,
        description="Anything about the window the two dates cannot carry: 'rolling', 'extended until further notice', a date given without a year",
    )
    academic_year: Optional[str] = Field(
        None, description="Target session in at most 9 characters: '2026-27'"
    )

    summary: Optional[str] = Field(
        None,
        description="One or two sentences a student would read in a directory listing, 20 to 500 characters",
    )
    description: str = Field(description="Fuller overview of the scholarship programme")

    scholarship_type: List[str] = Field(
        default_factory=list, description="Cash, Tuition Waiver, Allowance, etc."
    )
    award_basis: Optional[AwardBasis] = Field(
        None, description="What the award is decided on: MERIT, NEED, MERIT_CUM_MEANS, CATEGORY or OTHER"
    )
    benefit_summary: str = Field(description="High-level monetary benefit summary, at most 300 characters")
    award_amount_min: Optional[float] = Field(
        None,
        description="Smallest amount a recipient gets, as a number in the currency below. For a single fixed award, set both min and max to it",
    )
    award_amount_max: Optional[float] = Field(
        None, description="Largest amount a recipient gets, as a number"
    )
    currency: Optional[str] = Field(None, description="Three-letter currency code, normally INR")
    is_renewable: Optional[bool] = Field(
        None, description="True only where the page says the award renews for later years"
    )
    award_amount_text: Optional[str] = Field(
        None, description="The award exactly as the page words it, with anything the numbers cannot carry"
    )
    benefits_breakdown: List[str] = Field(
        default_factory=list, description="Itemised breakdown of benefits by category"
    )

    eligibility_summary: str = Field(description="Summary of key qualifications needed")
    who_qualifies: EligibilityCriteria = Field(description="Structured eligibility conditions")

    application_process: ApplicationProcess = Field(description="Application links and step-by-step guide")
    documents_required: Optional[List[str]] = Field(
        default_factory=list, description="Required documents, at most 20, each a short phrase"
    )
    contacts: List[Contact] = Field(
        default_factory=list, description="Every email, phone and WhatsApp number the page gives"
    )
    logo_url: Optional[str] = Field(
        None,
        description=(
            "The sponsor's own logo or emblem, chosen from the images listed with the page - "
            "their mark, not a photograph, a banner, an icon or a poster. Copy the URL exactly. "
            "Null if none of the images is the sponsor's mark"
        ),
    )
    important_notes: Optional[str] = Field(
        None, description="Terms and anything else worth keeping that no field above holds"
    )


SYSTEM_INSTRUCTION = """
You are an expert scholarship data extraction AI.

Extract ONLY what the supplied content actually states. You are reading a live
page that may describe a newer edition of a scholarship than you remember, so
never fall back on prior knowledge of the programme, and never infer what a
scholarship "usually" offers. If the content does not state a value, return null
for it (or an empty list). Returning null is correct and expected; returning a
plausible-looking guess is a serious error.

Several fields ask for a stated fact in a fixed form - a date as YYYY-MM-DD, an
amount as a number, a state as its two-letter code, a level of study as one of
the four allowed values. Converting what the page says into that form is part of
the job and is not a guess. Inventing the fact because the shape wants filling
is. If a deadline is given as "April 2027" with no day, leave closes_at null and
put the phrase in dates_text; if an award is "up to Rs 50,000", the maximum is
50000 and the minimum is null.

The *_text fields exist for everything the structured value cannot carry -
conditions on an amount, a window that is rolling, the sponsor's own description
of itself. Fill them from the page's own words rather than stretching a
structured field to hold prose.

For eligibility, restrictive lists are null unless the scheme is actually
restricted: a scholarship open to students of any gender has genders null, not
all four values listed, and one open across India has state_codes null rather
than all thirty-six. Anything the structured fields cannot express belongs in
other_conditions or eligibility_summary, in the page's own terms.

Copy names, amounts, percentages, academic years and dates exactly as the
content gives them - do not round or update them. Where the content lists
several awards or sub-schemes, reflect the full range it states.

Break links, application steps, contacts and document lists out into their
structured formats.
"""
