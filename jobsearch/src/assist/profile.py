"""The user's application data, and how it is kept honest as it changes.

Four files in the profile directory (gitignored; ADR-017):

    profile.yaml      hand-written. Who the user is: name, contact, links, work,
                      education. Structured from the resume, reviewed by the user.
    answers.yaml      hand-written. Screening answers, each with the question
                      phrasings it answers. Grows as new questions turn up.
    resume-pin.yaml   TOOL-written by `profile pin`. Which PDF is current, and the
                      sha256 of every PDF ever pinned.
    unanswered.yaml   TOOL-written by Assist. Questions met on a real form that no
                      answer matched: the inbox answers.yaml grows from.

The pin is its own file so that no code ever rewrites profile.yaml or
answers.yaml -- both carry the user's comments, and a YAML round-trip drops them.

**Drift.** Every PDF in the folder must have a sha256 in the pin history, and the
current one must still hash to what was pinned. A new resume dropped in, or the
current one edited in place, is drift, and Assist refuses to fill until the
profile is updated and the PDF pinned: a new PDF sent beside old typed fields is
a plausible-but-wrong application (ADR-003's class of failure). File dates are
not used -- a copy can carry an old mtime.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PROFILE = "profile.yaml"
ANSWERS = "answers.yaml"
PIN = "resume-pin.yaml"
INBOX = "unanswered.yaml"
SCHEMA = 1

# Every key profile.yaml may carry. Unknown keys are REFUSED: a typo such as
# `basic:` would otherwise load cleanly and silently fill nothing.
PROFILE_KEYS = {"schema", "basics", "links", "current", "work", "education", "skills"}
BASICS_KEYS = {"first_name", "last_name", "preferred_name", "email", "phone",
               "headline", "location", "city", "country"}
LINK_KEYS = {"linkedin", "github", "website", "portfolio", "twitter"}
CURRENT_KEYS = {"company", "title"}
ANSWER_KEYS = {"id", "match", "contains", "answer", "options", "reviewed",
               "expires_days", "free_text", "note"}


class ProfileError(Exception):
    """A profile file is missing or malformed. Fix the file; nothing is guessed."""


class ProfileDrift(ProfileError):
    """The resume on disk is not the one the profile was reviewed against."""


# --------------------------------------------------------------------------- #
# Text normalisation -- the one definition of "the same question"
# --------------------------------------------------------------------------- #

_TRAILING = re.compile(r"\s*(required|optional)$")


def normalise(text: Any) -> str:
    """Case, accents, punctuation and whitespace folded; a trailing
    "required"/"optional" marker dropped. Nothing fuzzier than that, on purpose:
    two questions that differ in a word are different questions."""
    s = unicodedata.normalize("NFKD", str(text or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return _TRAILING.sub("", s).strip()


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_yaml(path: Path, required: bool = True) -> Any:
    if not path.exists():
        if required:
            raise ProfileError(
                f"{path} not found. Copy it from docs/resume.example/ and fill it "
                f"in -- see docs/engineering/resume-and-answers.md.")
        return None
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ProfileError(f"{path.name} is not valid YAML: {e}") from e


def _write_yaml(path: Path, header: str, data: Any) -> None:
    """Tool-written files only. Temp-and-rename so a crash never leaves half a file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
                   encoding="utf-8")
    tmp.replace(path)


def _check_keys(where: str, got: dict, allowed: set[str]) -> None:
    unknown = sorted(set(got) - allowed)
    if unknown:
        raise ProfileError(f"{where}: unknown key(s) {unknown}. Allowed: {sorted(allowed)}")


def _as_date(v: Any, where: str) -> dt.date | None:
    if v in (None, ""):
        return None
    if isinstance(v, dt.date):
        return v
    try:
        return dt.date.fromisoformat(str(v))
    except ValueError:
        raise ProfileError(f"{where}: {v!r} is not a YYYY-MM-DD date") from None


# --------------------------------------------------------------------------- #
# profile.yaml
# --------------------------------------------------------------------------- #

@dataclass
class Profile:
    raw: dict

    def value(self, key: str) -> str | None:
        """A standard field by its match-vocabulary key, or None if blank."""
        b = self.raw.get("basics") or {}
        links = self.raw.get("links") or {}
        cur = self.raw.get("current") or {}
        first, last = b.get("first_name"), b.get("last_name")
        v = {
            "first_name": b.get("preferred_name") or first,
            "legal_first_name": first,
            "last_name": last,
            "full_name": " ".join(str(x) for x in (first, last) if x) or None,
            "email": b.get("email"),
            "phone": b.get("phone"),
            "location": b.get("location"),
            "city": b.get("city"),
            "country": b.get("country"),
            "headline": b.get("headline"),
            "linkedin": links.get("linkedin"),
            "github": links.get("github"),
            "website": links.get("website") or links.get("portfolio"),
            "portfolio": links.get("portfolio") or links.get("website"),
            "twitter": links.get("twitter"),
            "current_company": cur.get("company"),
            "current_title": cur.get("title"),
        }.get(key)
        s = str(v).strip() if v is not None else ""
        return s or None


def load_profile(d: Path) -> Profile:
    raw = _read_yaml(d / PROFILE)
    if not isinstance(raw, dict):
        raise ProfileError(f"{PROFILE} must be a mapping")
    _check_keys(PROFILE, raw, PROFILE_KEYS)
    if raw.get("schema") != SCHEMA:
        raise ProfileError(f"{PROFILE}: schema must be {SCHEMA}, got {raw.get('schema')!r}")
    for key, allowed in (("basics", BASICS_KEYS), ("links", LINK_KEYS),
                         ("current", CURRENT_KEYS)):
        sub = raw.get(key) or {}
        if not isinstance(sub, dict):
            raise ProfileError(f"{PROFILE}: {key} must be a mapping")
        _check_keys(f"{PROFILE}: {key}", sub, allowed)
    b = raw.get("basics") or {}
    for k in ("first_name", "last_name", "email"):
        if not str(b.get(k) or "").strip():
            raise ProfileError(f"{PROFILE}: basics.{k} is required")
    return Profile(raw)


# --------------------------------------------------------------------------- #
# answers.yaml
# --------------------------------------------------------------------------- #

@dataclass
class Answer:
    id: str
    match: list[str]              # normalised; the question must EQUAL one
    contains: list[str]           # normalised; the question must CONTAIN one
    answer: Any                   # None = a known question not yet answered
    options: dict[str, list[str]] = field(default_factory=dict)
    reviewed: dt.date | None = None
    expires_days: int | None = None
    free_text: bool = False

    def matches(self, key: str) -> bool:
        return key in self.match or any(c in key for c in self.contains)

    @property
    def blank(self) -> bool:
        return self.answer is None or (isinstance(self.answer, str)
                                       and not self.answer.strip())

    def stale(self, today: dt.date) -> bool:
        """A volatile answer (salary, notice period) past its review window is
        not filled. Never reviewed at all counts as past it."""
        if not self.expires_days:
            return False
        return self.reviewed is None or (today - self.reviewed).days > self.expires_days


def load_answers(d: Path) -> list[Answer]:
    raw = _read_yaml(d / ANSWERS)
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
        raise ProfileError(f"{ANSWERS}: must be a mapping with schema: {SCHEMA}")
    _check_keys(ANSWERS, raw, {"schema", "answers"})
    out: list[Answer] = []
    seen: set[str] = set()
    for i, a in enumerate(raw.get("answers") or []):
        where = f"{ANSWERS}: answers[{i}]"
        if not isinstance(a, dict):
            raise ProfileError(f"{where} is not a mapping")
        _check_keys(where, a, ANSWER_KEYS)
        aid = str(a.get("id") or "").strip()
        if not aid:
            raise ProfileError(f"{where} needs an id")
        if aid in seen:
            raise ProfileError(f"{where}: duplicate id {aid!r}")
        seen.add(aid)
        match = [normalise(m) for m in _strings(a.get("match"), where, "match")]
        contains = [normalise(c) for c in _strings(a.get("contains"), where, "contains")]
        if not any(match) and not any(contains):
            raise ProfileError(f"{where} ({aid}): needs at least one match or contains")
        opts = a.get("options") or {}
        if not isinstance(opts, dict):
            raise ProfileError(f"{where} ({aid}): options must map answer -> synonyms")
        out.append(Answer(
            id=aid, match=[m for m in match if m], contains=[c for c in contains if c],
            answer=a.get("answer"),
            options={str(k): _strings(v, where, f"options.{k}") for k, v in opts.items()},
            reviewed=_as_date(a.get("reviewed"), f"{where} ({aid}).reviewed"),
            expires_days=int(a["expires_days"]) if a.get("expires_days") else None,
            free_text=bool(a.get("free_text")),
        ))
    return out


def _strings(v: Any, where: str, key: str) -> list[str]:
    if v in (None, ""):
        return []
    if isinstance(v, str):
        return [v]
    if isinstance(v, list) and all(isinstance(x, (str, int, float)) for x in v):
        return [str(x) for x in v]
    raise ProfileError(f"{where}: {key} must be a string or a list of strings")


# --------------------------------------------------------------------------- #
# resume-pin.yaml -- which PDF is current, and every PDF ever pinned
# --------------------------------------------------------------------------- #

_PIN_HEADER = ("# Written by `python -m src.cli profile pin`. Do not edit by hand.\n"
               "# history keeps every resume ever pinned, so a past application can\n"
               "# be traced to the exact PDF it sent. See ADR-017.\n")


def load_pin(d: Path) -> dict:
    raw = _read_yaml(d / PIN, required=False) or {}
    if not isinstance(raw, dict):
        raise ProfileError(f"{PIN} is malformed; re-run `profile pin <pdf>`")
    raw.setdefault("history", [])
    return raw


@dataclass
class Resume:
    path: Path
    sha256: str

    @property
    def tag(self) -> str:
        """What status_event.resume records: enough to find the exact file."""
        return f"{self.path.name}@{self.sha256[:8]}"


def drift(d: Path) -> list[str]:
    """Every reason the profile cannot be trusted right now. Empty = clean."""
    pin = load_pin(d)
    current = pin.get("current")
    if not current:
        return [f"no resume pinned yet. Run `python -m src.cli profile pin <pdf>`."]
    problems: list[str] = []
    known = {h.get("sha256") for h in pin["history"]}
    cur = d / current
    if not cur.exists():
        problems.append(f"the pinned resume {current} is no longer in {d}")
    elif sha256(cur) != pin.get("sha256"):
        problems.append(f"{current} changed since it was pinned")
    for pdf in sorted(d.glob("*.pdf")):
        if pdf.name != current and sha256(pdf) not in known:
            problems.append(f"{pdf.name} is new and has not been pinned")
    return problems


def current_resume(d: Path) -> Resume:
    """The pinned PDF, or ProfileDrift if the folder disagrees with the pin."""
    problems = drift(d)
    if problems:
        raise ProfileDrift(
            "the resume changed and the profile has not been re-pinned:\n  - "
            + "\n  - ".join(problems)
            + "\nUpdate profile.yaml to match, then `python -m src.cli profile pin "
              "<pdf>`. See docs/engineering/resume-and-answers.md.")
    pin = load_pin(d)
    return Resume(d / pin["current"], pin["sha256"])


def pin(d: Path, pdf_name: str, today: dt.date | None = None) -> Resume:
    """Make `pdf_name` the current resume and add it to the history.

    Deliberately does not touch profile.yaml. It is the user's statement that
    profile.yaml now matches this PDF -- the CLI prints the profile first so
    that statement is made looking at it.
    """
    pdf = d / Path(pdf_name).name
    if not pdf.exists() or pdf.suffix.lower() != ".pdf":
        raise ProfileError(f"{pdf} is not a PDF in {d}")
    load_profile(d)                      # never pin against a broken profile
    digest = sha256(pdf)
    when = (today or dt.date.today()).isoformat()
    state = load_pin(d)
    history = [h for h in state["history"] if h.get("sha256") != digest]
    history.append({"file": pdf.name, "sha256": digest, "pinned_on": when})
    # Other unpinned PDFs are NOT adopted. Pinning the old resume by mistake
    # after dropping in a new one would otherwise mark the new one as known and
    # silence the drift it should raise. Each PDF becomes known only by being
    # pinned itself (older versions: pin them first, the current one last).
    _write_yaml(d / PIN, _PIN_HEADER,
                {"current": pdf.name, "sha256": digest, "pinned_on": when,
                 "history": history})
    return Resume(pdf, digest)


# --------------------------------------------------------------------------- #
# unanswered.yaml -- the inbox answers.yaml grows from
# --------------------------------------------------------------------------- #

_INBOX_HEADER = (
    "# Written by Assist. Questions seen on real application forms that no entry\n"
    "# in answers.yaml matched. To clear one: add an answer to answers.yaml whose\n"
    "# `match` is the question text below. The next Assist run or\n"
    "# `python -m src.cli answers pending` removes it from here. Do not edit.\n")
MAX_JOBS_PER_QUESTION = 10


def load_inbox(d: Path) -> list[dict]:
    raw = _read_yaml(d / INBOX, required=False) or {}
    return list(raw.get("questions") or []) if isinstance(raw, dict) else []


def record_unanswered(d: Path, fields: list[dict], job_id: str, ats: str,
                      today: dt.date | None = None) -> None:
    """Merge questions met on one form into the inbox.

    `fields`: {label, type, options, reason} for each question left blank
    because no answer matched it. Keyed by normalised label, so the same
    question on fifty forms is one entry with a count, not fifty.
    """
    if not fields:
        return
    when = (today or dt.date.today()).isoformat()
    inbox = {q["key"]: q for q in load_inbox(d)}
    for f in fields:
        key = normalise(f.get("label"))
        if not key:
            continue
        q = inbox.get(key)
        if q is None:
            q = inbox[key] = {"key": key, "question": str(f.get("label")).strip(),
                              "type": f.get("type"), "options": f.get("options") or [],
                              "reason": f.get("reason"), "ats": [], "seen": 0,
                              "first_seen": when, "jobs": []}
        q["seen"] = int(q.get("seen") or 0) + (0 if job_id in q["jobs"] else 1)
        q["last_seen"] = when
        q["reason"] = f.get("reason") or q.get("reason")
        if ats not in q["ats"]:
            q["ats"].append(ats)
        if job_id not in q["jobs"]:
            q["jobs"] = (q["jobs"] + [job_id])[-MAX_JOBS_PER_QUESTION:]
        # A select's options can differ per company; keep the union so the
        # user writing the answer sees every spelling it has to satisfy.
        for o in f.get("options") or []:
            if o not in q["options"]:
                q["options"].append(o)
    _save_inbox(d, list(inbox.values()))


def prune_inbox(d: Path, answers: list[Answer]) -> int:
    """Drop inbox questions that an answer with a value now matches."""
    qs = load_inbox(d)
    keep = [q for q in qs
            if not any(a.matches(q["key"]) and not a.blank for a in answers)]
    if len(keep) != len(qs):
        _save_inbox(d, keep)
    return len(qs) - len(keep)


def _save_inbox(d: Path, questions: list[dict]) -> None:
    questions.sort(key=lambda q: (-int(q.get("seen") or 0), q["key"]))
    _write_yaml(d / INBOX, _INBOX_HEADER, {"questions": questions})


def pending(d: Path, today: dt.date | None = None) -> dict:
    """Everything answers.yaml needs a human for, in one place."""
    today = today or dt.date.today()
    answers = load_answers(d)
    prune_inbox(d, answers)
    return {
        "unanswered": load_inbox(d),
        "blank": [a.id for a in answers if a.blank],
        "stale": [{"id": a.id, "reviewed": a.reviewed.isoformat() if a.reviewed else None,
                   "expires_days": a.expires_days}
                  for a in answers if not a.blank and a.stale(today)],
    }
