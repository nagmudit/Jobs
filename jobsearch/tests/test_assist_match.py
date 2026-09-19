"""What goes into an application: form fields -> fill plan.

Every rule here exists to keep a wrong answer out of a real application
(ADR-017). The tests are phrased as the failure they prevent. Forms are
synthetic descriptors shaped like the ones the Greenhouse / Ashby / Lever
extractors produce -- the browser is not needed to decide what to type.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from src.assist.match import Field, plan
from src.assist.profile import Answer, Profile, normalise

TODAY = dt.date(2026, 9, 19)
RESUME = Path("/r/resume.pdf")

PROFILE = Profile({
    "schema": 1,
    "basics": {"first_name": "Ada", "last_name": "Example", "email": "ada@example.com",
               "phone": "+1 555 0100", "location": "Springfield, USA"},
    "links": {"linkedin": "https://linkedin.com/in/ada", "github": None},
    "current": {"company": "Example Corp", "title": "Engineer"},
})


def ans(id, answer, match=(), contains=(), **kw):
    return Answer(id=id, match=[normalise(m) for m in match],
                  contains=[normalise(c) for c in contains], answer=answer, **kw)


def one(field, answers=()):
    [f] = plan([field], PROFILE, list(answers), RESUME, TODAY)
    return f


# --- standard fields ------------------------------------------------------

@pytest.mark.parametrize("label,expected", [
    ("First Name", "Ada"), ("Last name *", "Example"), ("Full name", "Ada Example"),
    ("Email Address", "ada@example.com"), ("Phone", "+1 555 0100"),
    ("LinkedIn Profile", "https://linkedin.com/in/ada"),
    ("Current Company", "Example Corp"), ("Location", "Springfield, USA"),
])
def test_standard_fields_fill_from_profile(label, expected):
    f = one(Field("x", label, "text"))
    assert (f.action, f.value) == ("fill", expected)
    assert f.source.startswith("profile:")


def test_extractor_hint_fills_a_label_the_table_does_not_know():
    f = one(Field("x", "Given", "text", hint="first_name"))
    assert f.value == "Ada"


def test_visible_label_beats_the_input_name():
    """Live, 2026-09-19: Ashby's `_systemfield_name` (full name) labelled
    "First Name". Trusting the name put "Ada Example" in First Name."""
    f = one(Field("x", "First Name", "text", hint="full_name"))
    assert f.value == "Ada"


def test_blank_profile_value_is_left_blank_not_invented():
    f = one(Field("x", "GitHub", "url"))
    assert (f.action, f.reason) == ("skip", "missing-profile")
    assert not f.inbox          # a profile gap is not an answers.yaml gap


def test_resume_upload_attaches_the_pinned_pdf():
    f = one(Field("x", "Resume/CV", "file"))
    assert (f.action, f.value, f.source) == ("upload", str(RESUME), "resume")


def test_cover_letter_is_never_filled_with_the_resume():
    assert one(Field("x", "Cover Letter", "file")).action == "skip"


def test_unknown_upload_is_left_alone_and_not_inboxed():
    """answers.yaml cannot hold a file; an inbox entry would be noise."""
    f = one(Field("x", "Writing sample", "file"))
    assert (f.action, f.reason, f.inbox) == ("skip", "upload", False)


# --- screening answers ----------------------------------------------------

def test_an_unknown_question_is_left_blank_and_inboxed():
    f = one(Field("x", "Do you have a security clearance?", "text"))
    assert (f.action, f.reason, f.inbox) == ("skip", "unmatched", True)


def test_exact_match_fills():
    a = ans("auth", "Yes", match=["Are you authorized to work in the US?"])
    f = one(Field("x", "Are you authorized to work in the US? *", "text"), [a])
    assert (f.action, f.value, f.source) == ("fill", "Yes", "answer:auth")


def test_near_miss_does_not_match():
    """No fuzzy matching: "the UK" is not "the US"."""
    a = ans("auth", "Yes", match=["Are you authorized to work in the US?"])
    assert one(Field("x", "Are you authorized to work in the UK?", "text"),
               [a]).reason == "unmatched"


def test_contains_matches_a_rephrasing():
    a = ans("sp", "No", contains=["require sponsorship"])
    f = one(Field("x", "Will you now or in the future require sponsorship?", "text"), [a])
    assert f.value == "No"


def test_two_answers_matching_is_ambiguous_not_a_pick():
    a1 = ans("auth", "Yes", contains=["authorized to work"])
    a2 = ans("sp", "No", contains=["sponsorship"])
    f = one(Field("x", "Are you authorized to work without sponsorship?", "text"),
            [a1, a2])
    assert (f.action, f.reason, f.inbox) == ("skip", "ambiguous", True)


def test_answer_outranks_the_standard_label_table():
    a = ans("loc", "Remote (India)", match=["Location"])
    assert one(Field("x", "Location", "text"), [a]).value == "Remote (India)"


def test_blank_answer_is_left_blank():
    f = one(Field("x", "Expected salary", "text"),
            [ans("sal", None, contains=["expected salary"])])
    assert (f.action, f.reason, f.inbox) == ("skip", "blank", True)


def test_stale_answer_is_not_sent():
    a = ans("sal", "100k", contains=["expected salary"],
            reviewed=dt.date(2026, 1, 1), expires_days=90)
    f = one(Field("x", "Expected salary", "text"), [a])
    assert (f.action, f.reason, f.inbox) == ("skip", "stale", False)


def test_fresh_expiring_answer_is_sent():
    a = ans("sal", "100k", contains=["expected salary"],
            reviewed=dt.date(2026, 9, 1), expires_days=90)
    assert one(Field("x", "Expected salary", "text"), [a]).value == "100k"


def test_textarea_needs_free_text_opt_in():
    a = ans("why", "Because.", match=["Why us?"])
    assert one(Field("x", "Why us?", "textarea"), [a]).reason == "free-text"
    a.free_text = True
    assert one(Field("x", "Why us?", "textarea"), [a]).action == "fill"


def test_boolean_answer_types_as_yes_no():
    a = ans("r", True, contains=["willing to relocate"])
    assert one(Field("x", "Are you willing to relocate?", "text"), [a]).value == "Yes"


# --- choices --------------------------------------------------------------

def test_select_picks_the_offered_option_exactly():
    a = ans("sp", "No", contains=["sponsorship"])
    f = one(Field("x", "Sponsorship?", "select", options=["Yes", "No"]), [a])
    assert (f.action, f.value) == ("select", "No")


def test_select_uses_listed_synonyms():
    a = ans("sp", "No", contains=["sponsorship"],
            options={"No": ["No, I will not require sponsorship"]})
    opts = ["Yes, I will require sponsorship", "No, I will not require sponsorship"]
    f = one(Field("x", "Sponsorship?", "radio", options=opts), [a])
    assert f.value == "No, I will not require sponsorship"


def test_answer_not_among_options_is_left_blank():
    """Never pick "the closest" option in a real application."""
    a = ans("sp", "No", contains=["sponsorship"])
    f = one(Field("x", "Sponsorship?", "select",
                  options=["Yes", "Not at this time", "Unsure"]), [a])
    assert (f.action, f.reason, f.inbox) == ("skip", "no-option", True)


def test_duplicate_equivalent_options_are_ambiguous():
    a = ans("g", "No", match=["Q"])
    assert one(Field("x", "Q", "select", options=["No", "no."]), [a]).action == "skip"


def test_multiselect_needs_every_value_offered():
    a = ans("tz", ["IST", "UTC"], match=["Time zones"])
    ok = one(Field("x", "Time zones", "multiselect", options=["IST", "UTC", "PST"]), [a])
    assert ok.value == ["IST", "UTC"]
    bad = one(Field("x", "Time zones", "multiselect", options=["IST", "PST"]), [a])
    assert bad.reason == "no-option"


def test_consent_checkbox_ticked_only_by_explicit_yes():
    label = "I agree to the privacy policy"
    assert one(Field("x", label, "checkbox")).action == "skip"
    assert one(Field("x", label, "checkbox"),
               [ans("c", "No", match=[label])]).action == "skip"
    assert one(Field("x", label, "checkbox"),
               [ans("c", True, match=[label])]).action == "check"


# --- custom widgets: shown, never clicked ---------------------------------

def test_combobox_answer_becomes_a_hint_not_an_action():
    a = ans("sp", "No", contains=["sponsorship"])
    f = one(Field("x", "Will you require sponsorship?", "combobox"), [a])
    assert (f.action, f.value) == ("hint", "No")


def test_button_group_hint_must_be_an_offered_option():
    a = ans("sp", "No", contains=["sponsorship"],
            options={"No": ["No, I will not"]})
    f = one(Field("x", "Sponsorship?", "buttons", options=["Yes", "No, I will not"]), [a])
    assert (f.action, f.value) == ("hint", "No, I will not")
    g = one(Field("x", "Sponsorship?", "buttons", options=["Maybe"]), [a])
    assert (g.action, g.reason) == ("skip", "no-option")


def test_unmatched_combobox_is_still_inboxed():
    f = one(Field("x", "Which office?", "combobox"))
    assert (f.reason, f.inbox) == ("unmatched", True)
