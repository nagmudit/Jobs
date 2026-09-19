"""Form fields -> a fill plan. Pure: no browser, no network, no clock but `today`.

The browser side (browser.py) turns a live form into `Field`s and applies the
returned `Fill`s. Everything that decides WHAT goes into an application lives
here, so it is tested offline against synthetic forms.

Rules, in order of how much they matter (ADR-017):

- **Never guess.** A field is filled only from an explicit source: a profile
  value, or an answer whose phrasing matches the question after `normalise`.
  No fuzzy matching. A select is filled only if the answer IS one of its
  options (or a synonym the user listed). Everything else is left blank with a
  reason, and the page outlines it.
- **Two answers matching one question is not a tie to break.** It is left blank
  as `ambiguous` so the user fixes answers.yaml instead of the tool picking.
- **Long free text needs an opt-in.** A textarea is filled only from an answer
  marked `free_text: true`, so a one-line answer never lands in a "why us" box.
- **A stale answer is not filled.** Salary or notice period past its
  `expires_days` window is surfaced for review instead.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .profile import Answer, Profile, normalise

TEXTUAL = {"text", "email", "tel", "url", "number", "textarea"}
CHOICE = {"select", "radio"}
MULTI = {"multiselect", "checkboxes"}
# Widgets that are not native inputs: Greenhouse's searchable dropdowns, Ashby's
# Yes/No buttons. Choosing in one needs a click or a key press, and the browser
# side makes neither (that is what makes "never submits" checkable). A matched
# answer is shown NEXT to the field for the human to pick instead.
CUSTOM = {"combobox", "buttons"}


@dataclass
class Field:
    ref: str                          # opaque handle the browser side resolves
    label: str
    type: str                         # TEXTUAL | CHOICE | MULTI | checkbox | file
    required: bool = False
    options: list[str] = field(default_factory=list)
    # A profile key the ATS extractor identified from the field's own id/name
    # (Greenhouse `first_name`, Lever `urls[LinkedIn]`). Used only when the
    # label is not in the table: the visible label is what the recruiter reads.
    hint: str | None = None


@dataclass
class Fill:
    field: Field
    action: str                       # fill | select | check | upload | hint | skip
    value: Any = None
    source: str | None = None         # "profile:email", "answer:<id>", "resume"
    reason: str | None = None         # why skipped; None when filled

    @property
    def inbox(self) -> bool:
        """Should this question be recorded in unanswered.yaml? Only when an
        answers.yaml entry would fix it -- not a missing profile value, not a
        stale answer (those are reported elsewhere)."""
        return self.reason in {"unmatched", "blank", "ambiguous", "no-option"}


# Profile keys by exact normalised label. What a human reads on the form.
STANDARD_LABELS: dict[str, str] = {
    **dict.fromkeys(["first name", "first", "given name", "preferred first name",
                     "preferred name"], "first_name"),
    "legal first name": "legal_first_name",
    **dict.fromkeys(["last name", "family name", "surname", "legal last name"],
                    "last_name"),
    **dict.fromkeys(["name", "full name", "legal name", "your name"], "full_name"),
    **dict.fromkeys(["email", "email address", "e mail", "your email"], "email"),
    **dict.fromkeys(["phone", "phone number", "mobile", "mobile number",
                     "mobile phone", "telephone"], "phone"),
    **dict.fromkeys(["location", "current location", "location city",
                     "where are you located"], "location"),
    "city": "city",
    "country": "country",
    **dict.fromkeys(["linkedin", "linkedin profile", "linkedin url",
                     "linkedin profile url"], "linkedin"),
    **dict.fromkeys(["github", "github url", "github profile"], "github"),
    **dict.fromkeys(["website", "personal website", "website url", "portfolio",
                     "portfolio url", "portfolio website", "other website"], "website"),
    **dict.fromkeys(["twitter", "twitter url", "x twitter"], "twitter"),
    **dict.fromkeys(["current company", "current employer", "company",
                     "current company name"], "current_company"),
    **dict.fromkeys(["current title", "current job title", "current role",
                     "current position"], "current_title"),
}
RESUME_LABELS = {"resume", "cv", "resume cv", "resume or cv", "upload resume",
                 "attach resume", "upload your resume", "resume upload"}
COVER_LABELS = {"cover letter", "cover letter upload", "upload cover letter"}


def plan(fields: list[Field], profile: Profile, answers: list[Answer],
         resume: Path, today: dt.date | None = None) -> list[Fill]:
    today = today or dt.date.today()
    return [_one(f, profile, answers, resume, today) for f in fields]


def _one(f: Field, profile: Profile, answers: list[Answer], resume: Path,
         today: dt.date) -> Fill:
    key = normalise(f.label)

    if f.type == "file":
        if f.hint == "resume" or key in RESUME_LABELS:
            return Fill(f, "upload", str(resume), "resume")
        if key in COVER_LABELS:
            return Fill(f, "skip", reason="cover-letter")
        # answers.yaml cannot supply a file, so this is never an inbox entry.
        return Fill(f, "skip", reason="upload")

    # An explicit answer is the user speaking about this exact question, so it
    # outranks the generic label table ("Location" -> "Remote, India", say).
    hits = [a for a in answers if key and a.matches(key)]
    if len(hits) > 1:
        return Fill(f, "skip", reason="ambiguous",
                    source=",".join(a.id for a in hits))
    if hits:
        return _from_answer(f, hits[0], today)

    # The label a human reads wins; the input's name is a fallback for labels
    # the table does not know. Ashby names its field `_systemfield_name` (full
    # name) yet one live form labelled it "First Name" -- trusting the name
    # typed "Ada Example" into First Name (2026-09-19).
    std = STANDARD_LABELS.get(key) or (f.hint if f.hint != "resume" else None)
    if std:
        v = profile.value(std)
        if v is None:
            return Fill(f, "skip", source=f"profile:{std}", reason="missing-profile")
        return _typed(f, v, f"profile:{std}", {})

    return Fill(f, "skip", reason="unmatched")


def _from_answer(f: Field, a: Answer, today: dt.date) -> Fill:
    src = f"answer:{a.id}"
    if a.blank:
        return Fill(f, "skip", source=src, reason="blank")
    if a.stale(today):
        return Fill(f, "skip", source=src, reason="stale")
    if f.type == "textarea" and not a.free_text:
        return Fill(f, "skip", source=src, reason="free-text")
    return _typed(f, a.answer, src, a.options)


def _typed(f: Field, value: Any, src: str, synonyms: dict[str, list[str]]) -> Fill:
    if f.type in CUSTOM:
        # When the options are readable (Ashby's buttons), the hint must still
        # be one of them -- a hint the human cannot find on screen is noise.
        if f.options:
            opt = _option(f.options, value, synonyms)
            return (Fill(f, "hint", opt, src) if opt is not None
                    else Fill(f, "skip", source=src, reason="no-option"))
        shown = ", ".join(map(_text, value)) if isinstance(value, list) else _text(value)
        return Fill(f, "hint", shown, src)
    if f.type in TEXTUAL:
        if isinstance(value, (list, dict)):
            return Fill(f, "skip", source=src, reason="no-option")
        return Fill(f, "fill", _text(value), src)
    if f.type in CHOICE:
        opt = _option(f.options, value, synonyms)
        return (Fill(f, "select", opt, src) if opt is not None
                else Fill(f, "skip", source=src, reason="no-option"))
    if f.type in MULTI:
        wanted = value if isinstance(value, list) else [value]
        picked = [_option(f.options, v, synonyms) for v in wanted]
        if not picked or any(p is None for p in picked):
            return Fill(f, "skip", source=src, reason="no-option")
        return Fill(f, "select", picked, src)
    if f.type == "checkbox":
        # A lone checkbox (consent, "I confirm...") is ticked only by an explicit
        # yes. Anything else leaves it for the human.
        if value is True or normalise(value) in {"yes", "true"}:
            return Fill(f, "check", True, src)
        return Fill(f, "skip", source=src, reason="no-option")
    return Fill(f, "skip", source=src, reason="unsupported")


def _text(v: Any) -> str:
    if isinstance(v, bool):
        return "Yes" if v else "No"
    return str(v)


def _option(options: list[str], value: Any, synonyms: dict[str, list[str]]) -> str | None:
    """The one offered option equal to the answer or a listed synonym, else None.

    Synonyms are looked up by the answer's own text, so
    `answer: "No"` + `options: {"No": ["I do not require sponsorship"]}` selects
    that longer option on a form that phrases it so. Two options normalising
    equal is ambiguous, and ambiguous is None.
    """
    text = _text(value)
    wanted = {normalise(text)} | {normalise(s) for s in synonyms.get(text, [])}
    wanted.discard("")
    hits = [o for o in options if normalise(o) in wanted]
    return hits[0] if len(hits) == 1 else None
