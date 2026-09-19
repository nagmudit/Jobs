"""The profile, the answers file, and keeping them honest as they change.

Assisted apply (ADR-017) types the user's details into real applications. The
two ways that goes wrong silently are both covered here:

- **The resume changes and the profile does not.** A new PDF attached next to
  an old title or employer is a plausible-but-wrong application. Every PDF in
  the folder must be pinned, and the pinned one must still hash the same, or
  Assist refuses (`ProfileDrift`).
- **A typo loads cleanly and fills nothing.** Unknown keys raise.

Runs against the committed fake-data templates in docs/resume.example/, copied
into a tmp dir -- never against the user's real, gitignored files.
"""

from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path

import pytest
import yaml

from src.assist import profile as P
from src.config import Config

EXAMPLE = Path(__file__).resolve().parents[2] / "docs" / "resume.example"
TODAY = dt.date(2026, 9, 19)


@pytest.fixture
def d(tmp_path):
    for f in ("profile.yaml", "answers.yaml"):
        shutil.copy(EXAMPLE / f, tmp_path / f)
    (tmp_path / "resume-v1.pdf").write_bytes(b"%PDF-1.4 version one")
    return tmp_path


def _edit(path: Path, fn):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    fn(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


# --------------------------------------------------------------------------- #
# The committed templates are valid -- they ARE the schema documentation
# --------------------------------------------------------------------------- #

def test_example_templates_load(d):
    prof = P.load_profile(d)
    assert prof.value("full_name") == "Ada Example"
    assert prof.value("website") is None          # blank stays None, not ""
    ids = [a.id for a in P.load_answers(d)]
    assert "sponsorship" in ids and "expected_salary" in ids


def test_unknown_profile_key_is_refused(d):
    _edit(d / "profile.yaml", lambda p: p.setdefault("basic", {"email": "x"}))
    with pytest.raises(P.ProfileError, match="unknown key"):
        P.load_profile(d)


def test_unknown_nested_key_is_refused(d):
    _edit(d / "profile.yaml", lambda p: p["links"].update(linkdin="typo"))
    with pytest.raises(P.ProfileError, match="linkdin"):
        P.load_profile(d)


def test_missing_required_basics_raise(d):
    _edit(d / "profile.yaml", lambda p: p["basics"].update(email=""))
    with pytest.raises(P.ProfileError, match="email"):
        P.load_profile(d)


def test_duplicate_answer_ids_raise(d):
    _edit(d / "answers.yaml", lambda a: a["answers"].append(dict(a["answers"][0])))
    with pytest.raises(P.ProfileError, match="duplicate id"):
        P.load_answers(d)


def test_answer_without_any_phrasing_raises(d):
    _edit(d / "answers.yaml",
          lambda a: a["answers"].append({"id": "orphan", "answer": "x"}))
    with pytest.raises(P.ProfileError, match="match or contains"):
        P.load_answers(d)


def test_missing_file_names_the_template(tmp_path):
    with pytest.raises(P.ProfileError, match="resume.example"):
        P.load_profile(tmp_path)


# --------------------------------------------------------------------------- #
# Drift: the resume changing is the lifecycle this was designed around
# --------------------------------------------------------------------------- #

def test_nothing_pinned_is_drift(d):
    assert P.drift(d) and "no resume pinned" in P.drift(d)[0]
    with pytest.raises(P.ProfileDrift):
        P.current_resume(d)


def test_pin_makes_it_clean(d):
    r = P.pin(d, "resume-v1.pdf", today=TODAY)
    assert P.drift(d) == []
    assert P.current_resume(d).tag == r.tag
    assert r.tag.startswith("resume-v1.pdf@") and len(r.tag.split("@")[1]) == 8


def test_new_pdf_dropped_in_is_drift(d):
    P.pin(d, "resume-v1.pdf", today=TODAY)
    (d / "resume-v2.pdf").write_bytes(b"%PDF-1.4 version two")
    problems = P.drift(d)
    assert any("resume-v2.pdf is new" in p for p in problems)
    with pytest.raises(P.ProfileDrift, match="re-pinned"):
        P.current_resume(d)


def test_pinned_pdf_edited_in_place_is_drift(d):
    """Same file name, different bytes -- a date check would miss this."""
    P.pin(d, "resume-v1.pdf", today=TODAY)
    (d / "resume-v1.pdf").write_bytes(b"%PDF-1.4 quietly edited")
    assert any("changed since it was pinned" in p for p in P.drift(d))


def test_pinned_pdf_deleted_is_drift(d):
    P.pin(d, "resume-v1.pdf", today=TODAY)
    (d / "resume-v1.pdf").unlink()
    assert any("no longer in" in p for p in P.drift(d))


def test_repin_keeps_the_old_version_in_history(d):
    """The full lifecycle: v1 pinned, v2 arrives, v2 pinned, v1 still known --
    and keeping the old PDF around is not drift."""
    v1 = P.pin(d, "resume-v1.pdf", today=TODAY)
    (d / "resume-v2.pdf").write_bytes(b"%PDF-1.4 version two")
    v2 = P.pin(d, "resume-v2.pdf", today=TODAY + dt.timedelta(days=30))
    assert P.drift(d) == []
    assert P.current_resume(d).tag == v2.tag
    hist = P.load_pin(d)["history"]
    assert [h["sha256"] for h in hist] == [v1.sha256, v2.sha256]


def test_pin_does_not_adopt_other_unpinned_pdfs(d):
    """Pinning the OLD resume by mistake must not silence the new one."""
    (d / "resume-v2.pdf").write_bytes(b"%PDF-1.4 version two")
    P.pin(d, "resume-v1.pdf", today=TODAY)
    assert any("resume-v2.pdf is new" in p for p in P.drift(d))


def test_pin_refuses_a_broken_profile(d):
    _edit(d / "profile.yaml", lambda p: p.update(schema=99))
    with pytest.raises(P.ProfileError, match="schema"):
        P.pin(d, "resume-v1.pdf")
    assert not (d / P.PIN).exists()


def test_pin_never_rewrites_the_hand_written_files(d):
    before = {f: (d / f).read_bytes() for f in ("profile.yaml", "answers.yaml")}
    P.pin(d, "resume-v1.pdf", today=TODAY)
    assert {f: (d / f).read_bytes() for f in before} == before


# --------------------------------------------------------------------------- #
# Answers growing: the inbox
# --------------------------------------------------------------------------- #

def test_unanswered_questions_merge_by_normalised_text(d):
    q = {"label": "Do you have a security clearance?", "type": "select",
         "options": ["Yes", "No"], "reason": "unmatched"}
    P.record_unanswered(d, [q], "greenhouse:1", "greenhouse", today=TODAY)
    P.record_unanswered(d, [{**q, "label": "Do you have a SECURITY clearance",
                             "options": ["No", "Prefer not to say"]}],
                        "ashby:2", "ashby", today=TODAY)
    [entry] = P.load_inbox(d)
    assert entry["seen"] == 2
    assert entry["ats"] == ["greenhouse", "ashby"]
    assert entry["options"] == ["Yes", "No", "Prefer not to say"]


def test_same_job_twice_counts_once(d):
    q = {"label": "Clearance?", "type": "text", "reason": "unmatched"}
    for _ in range(3):
        P.record_unanswered(d, [q], "greenhouse:1", "greenhouse", today=TODAY)
    assert P.load_inbox(d)[0]["seen"] == 1


def test_answering_a_question_drains_it_from_the_inbox(d):
    P.record_unanswered(d, [{"label": "Do you have a security clearance?",
                             "type": "text", "reason": "unmatched"}],
                        "greenhouse:1", "greenhouse", today=TODAY)
    assert P.pending(d, TODAY)["unanswered"]
    _edit(d / "answers.yaml", lambda a: a["answers"].append(
        {"id": "clearance", "match": ["Do you have a security clearance?"],
         "answer": "No"}))
    assert P.pending(d, TODAY)["unanswered"] == []


def test_a_blank_answer_does_not_drain_the_inbox(d):
    """Writing the question into answers.yaml without an answer is not done."""
    P.record_unanswered(d, [{"label": "Clearance?", "type": "text",
                             "reason": "unmatched"}], "g:1", "greenhouse", today=TODAY)
    _edit(d / "answers.yaml", lambda a: a["answers"].append(
        {"id": "clearance", "match": ["Clearance?"], "answer": None}))
    todo = P.pending(d, TODAY)
    assert todo["unanswered"] and "clearance" in todo["blank"]


def test_stale_answers_are_listed(d):
    # example notice_period: reviewed 2026-09-01, expires after 60 days
    assert P.pending(d, dt.date(2026, 10, 1))["stale"] == []
    stale = P.pending(d, dt.date(2026, 11, 15))["stale"]
    assert [s["id"] for s in stale] == ["notice_period"]


def test_expiring_answer_never_reviewed_is_stale(d):
    _edit(d / "answers.yaml", lambda a: a["answers"].append(
        {"id": "ctc", "contains": ["current ctc"], "answer": "10", "expires_days": 30}))
    assert "ctc" in [s["id"] for s in P.pending(d, TODAY)["stale"]]


def test_normalise_folds_only_cosmetics():
    assert P.normalise("Are you authorized to work in the U.S.?*") == \
        "are you authorized to work in the u s"
    assert P.normalise("LinkedIn Profile (required)") == "linkedin profile"
    assert P.normalise("Café") == "cafe"
    assert P.normalise("Current salary") != P.normalise("Expected salary")


# --------------------------------------------------------------------------- #
# Config: the host allowlist is exact and validated at load
# --------------------------------------------------------------------------- #

def _targets(tmp_path, assist):
    p = tmp_path / "targets.yaml"
    p.write_text(yaml.safe_dump({"roles": ["x"], "locations": ["anywhere"],
                                 "assist": assist}), encoding="utf-8")
    return p


@pytest.mark.parametrize("bad", ["https://jobs.lever.co", "*.greenhouse.io",
                                 "jobs.lever.co/path", "localhost", ".lever.co",
                                 "jobs.lever.co:443"])
def test_assist_host_must_be_a_bare_hostname(tmp_path, bad):
    with pytest.raises(ValueError, match="bare hostname"):
        Config.load(_targets(tmp_path, {"hosts": [bad]}))


def test_assist_config_parses(tmp_path, monkeypatch):
    monkeypatch.delenv("JOBSEARCH_PROFILE_DIR", raising=False)
    where = tmp_path / "resume"
    cfg = Config.load(_targets(tmp_path, {"profile_dir": str(where),
                                          "hosts": ["Jobs.Lever.co"]}))
    assert cfg.assist_hosts == ["jobs.lever.co"]
    assert cfg.assist_profile_dir == where


def test_assist_absent_means_unconfigured(tmp_path, monkeypatch):
    monkeypatch.delenv("JOBSEARCH_PROFILE_DIR", raising=False)
    p = tmp_path / "targets.yaml"
    p.write_text("roles: [x]\nlocations: [anywhere]\n", encoding="utf-8")
    cfg = Config.load(p)
    assert cfg.assist_hosts == [] and cfg.assist_profile_dir is None
