"""The conduct guards around the second path to the network (ADR-017).

Assist drives a real browser outside `Fetcher.get`, so the rules that make that
acceptable are tested here as hard as the crawl's conduct rules are:

- it never submits, clicks or presses a key (fake page records every call);
- the page scripts it injects cannot act or reach the network (source scan);
- headed only, no stealth (source scan of the whole package);
- only the clicked job's own form, only on an exactly-allowlisted host;
- a challenge interstitial means nothing is filled;
- drift refuses before a browser is ever opened;
- hosted read-only has no route at all.

The ONLY fake is the browser page -- the boundary this code talks across, as
the network is for the fetcher. Profile, matcher, store and route are real.
"""

from __future__ import annotations

import datetime as dt
import re
import shutil
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from src import store as S
from src.assist import browser as B
from src.assist import match as M
from src.assist import profile as P
from src.config import Config
from src.web.app import create_app

HOSTS = ["job-boards.greenhouse.io", "boards.greenhouse.io", "jobs.ashbyhq.com",
         "jobs.lever.co", "apply.workable.com"]
EXAMPLE = Path(__file__).resolve().parents[2] / "docs" / "resume.example"
PKG = Path(B.__file__).resolve().parent


# --------------------------------------------------------------------------- #
# form_url: which page, on which host
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("apply_url,job_id,expected", [
    ("https://jobs.ashbyhq.com/aios/753825b2", "ashby:aios/753825b2",
     "https://jobs.ashbyhq.com/aios/753825b2/application"),
    ("https://jobs.ashbyhq.com/aios/753825b2/application", "ashby:x",
     "https://jobs.ashbyhq.com/aios/753825b2/application"),
    ("https://jobs.lever.co/sambatv/8607b6ab/apply", "lever:x",
     "https://jobs.lever.co/sambatv/8607b6ab/apply"),
    ("https://jobs.lever.co/sambatv/8607b6ab", "lever:x",
     "https://jobs.lever.co/sambatv/8607b6ab/apply"),
    ("https://apply.workable.com/j/2E5C4DBCD2", "workable:x",
     "https://apply.workable.com/j/2E5C4DBCD2/apply"),
    ("https://job-boards.greenhouse.io/blend/jobs/6141420004", "greenhouse:blend/6141420004",
     "https://job-boards.greenhouse.io/blend/jobs/6141420004"),
    # Company careers page embedding Greenhouse: served from Greenhouse's host.
    ("https://bitwarden.com/careers/7872037003/?gh_jid=7872037003",
     "greenhouse:bitwarden/7872037003",
     "https://job-boards.greenhouse.io/embed/job_app?for=bitwarden&token=7872037003"),
    ("http://jobs.lever.co/a/b/apply", "lever:x", "https://jobs.lever.co/a/b/apply"),
])
def test_form_url(apply_url, job_id, expected):
    t = B.form_url(apply_url, job_id, HOSTS)
    assert t is not None and t.url == expected


@pytest.mark.parametrize("apply_url,job_id", [
    ("https://wellfound.com/jobs/123", "wellfound:123"),
    ("https://himalayas.app/companies/x/jobs/y", "himalayas:y"),
    ("https://example.com/careers/1", "remoteok:1"),          # not greenhouse
    ("https://jobs.lever.co.evil.example/a/b", "lever:x"),     # suffix trick
    ("https://jobs.lever.co@evil.example/a/b", "lever:x"),     # userinfo trick
    ("", "ashby:x"),
    (None, "lever:x"),
])
def test_form_url_refuses_anything_off_the_allowlist(apply_url, job_id):
    assert B.form_url(apply_url, job_id, HOSTS) is None


def test_allowlist_is_the_last_word():
    """An ATS the code knows, but the user removed from assist.hosts, is refused."""
    assert B.form_url("https://jobs.lever.co/a/b", "lever:x",
                      [h for h in HOSTS if h != "jobs.lever.co"]) is None
    assert B.form_url("https://x.com/c/1", "greenhouse:acme/1",
                      ["jobs.lever.co"]) is None


# --------------------------------------------------------------------------- #
# The fake page: records EVERY call, including ones that must never happen
# --------------------------------------------------------------------------- #

class FakeLocator:
    def __init__(self, page, sel):
        self.page, self.sel = page, sel

    def __getattr__(self, name):
        def call(*a, **kw):
            self.page.calls.append((name, self.sel, a, kw))
        return call


class FakePage:
    def __init__(self, fields, challenge=None):
        self.fields = fields
        self.challenge = challenge or {"interstitial": False, "widget": False}
        self.calls: list[tuple] = []
        self.url = None

    def goto(self, url, **kw):
        self.url = url

    def wait_for_selector(self, *a, **kw):
        pass

    def bring_to_front(self):
        pass

    def wait_for_load_state(self, state, **kw):
        self.settled = state

    def is_closed(self):
        return False

    def locator(self, sel):
        return FakeLocator(self, sel)

    def evaluate(self, script, arg=None):
        self.calls.append(("evaluate", script, arg))
        if script is B.CHALLENGE_JS:
            return self.challenge
        if script is B.EXTRACT_JS:
            return self.fields
        return True

    def __getattr__(self, name):      # click, keyboard, press, dispatch_event...
        def call(*a, **kw):
            self.calls.append((name, a, kw))
        return call


def raw(ref, label, type="text", name="", options=(), option_refs=None, required=False):
    return {"ref": ref, "label": label, "type": type, "name": name,
            "options": list(options), "option_refs": option_refs or {},
            "required": required}


FORM = [
    raw("0", "First Name", name="first_name", required=True),
    raw("1", "Email", "email", name="email", required=True),
    raw("2", "Resume/CV", "file", name="resume", required=True),
    raw("3", "Will you now or in the future require sponsorship?", "radio",
        options=["Yes", "No"], option_refs={"Yes": "3", "No": "4"}),
    raw("5", "Do you have a security clearance?", "text"),
    raw("6", "Which office would you prefer?", "combobox"),
]


def test_apply_plan_only_changes_values():
    page = FakePage(FORM)
    fields, orefs = B.extract(page)
    prof = P.Profile({"basics": {"first_name": "Ada", "email": "a@example.com"}})
    answers = [P.Answer("sp", [], ["require sponsorship"], "No")]
    fills = M.plan(fields, prof, answers, Path("/r/cv.pdf"), dt.date(2026, 9, 19))
    B.apply_plan(page, fills, orefs)

    acted = {c[0] for c in page.calls if c[0] != "evaluate"}
    assert acted <= B.ALLOWED_PAGE_CALLS, f"forbidden page calls: {acted - B.ALLOWED_PAGE_CALLS}"
    assert acted == {"fill", "set_input_files", "check"}
    scripts = {c[1] for c in page.calls if c[0] == "evaluate"}
    assert scripts <= set(B.PAGE_SCRIPTS)
    # the radio ticked is the "No" option's own control
    assert ("check", '[data-js-ref="4"]', (), {}) in page.calls


def test_uploads_happen_after_every_other_field():
    """Greenhouse's uploader initialises late; attaching first crashed it."""
    # Greenhouse's own order: Resume/CV, then LinkedIn below it.
    page = FakePage([raw("0", "First Name", name="first_name"),
                     raw("1", "Resume/CV", "file", name="resume"),
                     raw("2", "LinkedIn Profile", "url")])
    fields, orefs = B.extract(page)
    prof = P.Profile({"basics": {"first_name": "Ada"},
                      "links": {"linkedin": "https://linkedin.com/in/ada"}})
    B.apply_plan(page, M.plan(fields, prof, [], Path("/r/cv.pdf")), orefs)
    acts = [c[0] for c in page.calls if c[0] in B.ALLOWED_PAGE_CALLS]
    assert acts == ["fill", "fill", "set_input_files"]


def test_every_skipped_field_is_marked_for_the_human():
    page = FakePage(FORM)
    fields, orefs = B.extract(page)
    fills = M.plan(fields, P.Profile({"basics": {}}), [], Path("/r.pdf"))
    B.apply_plan(page, fills, orefs)
    marks = {c[2][0]: c[2][1] for c in page.calls
             if c[0] == "evaluate" and c[1] is B.MARK_JS}
    assert set(marks) == {f.ref for f in fields}
    assert marks["5"] == "blank" and marks["2"] == "filled"


# --------------------------------------------------------------------------- #
# Source scans: what the code CANNOT do, checked on the text itself
# --------------------------------------------------------------------------- #

ACTING_JS = re.compile(
    r"\.click\s*\(|\.submit\s*\(|requestSubmit|dispatchEvent|KeyboardEvent|MouseEvent|PointerEvent|"
    r"\.focus\s*\(|fetch\s*\(|XMLHttpRequest|sendBeacon|location\s*=|\.href\s*=|"
    r"window\.open", re.I)


@pytest.mark.parametrize("script", B.PAGE_SCRIPTS,
                         ids=["challenge", "extract", "mark", "banner"])
def test_injected_scripts_cannot_act_or_reach_the_network(script):
    assert not ACTING_JS.search(script), ACTING_JS.search(script).group(0)


def _package_source() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in PKG.glob("*.py"))


def test_headed_only():
    assert B.HEADLESS is False
    src = _package_source()
    assert "headless=HEADLESS" in src
    assert not re.search(r"headless\s*=\s*True", src)


@pytest.mark.parametrize("forbidden", [
    r"\.click\s*\(", r"\.press\s*\(", r"\.tap\s*\(", r"\.dispatch_event\s*\(",
    r"keyboard", r"\.mouse\b", r"add_init_script", r"webdriver", r"user_agent\s*=",
    r"proxy", r"stealth", r"captcha_?solver", r"2captcha", r"anticaptcha",
])
def test_no_acting_or_stealth_calls_anywhere_in_the_package(forbidden):
    code = "\n".join(line for line in _package_source().splitlines()
                     if not line.strip().startswith("#"))
    # Docstrings may NAME what is forbidden; strip them before scanning.
    code = re.sub(r'"""(.|\n)*?"""', "", code)
    assert not re.search(forbidden, code, re.I)


# --------------------------------------------------------------------------- #
# The route, end to end, with only the page faked
# --------------------------------------------------------------------------- #

@pytest.fixture
def profile_dir(tmp_path):
    d = tmp_path / "resume"
    d.mkdir()
    for f in ("profile.yaml", "answers.yaml"):
        shutil.copy(EXAMPLE / f, d / f)
    (d / "cv.pdf").write_bytes(b"%PDF-1.4 cv")
    P.pin(d, "cv.pdf")
    return d


def _cfg(tmp_path, profile_dir):
    p = tmp_path / "targets.yaml"
    p.write_text(yaml.safe_dump({"roles": ["x"], "locations": ["anywhere"],
                                 "assist": {"profile_dir": str(profile_dir),
                                            "hosts": HOSTS}}), encoding="utf-8")
    cfg = Config.load(p)
    cfg.db_path = tmp_path / "t.db"
    conn = S.connect(cfg.db_path)
    recent = dt.datetime.now(dt.timezone.utc).isoformat()
    S.upsert_job(conn, "acme/1", "acme",
                 {"title": "AI Engineer", "first_published": recent,
                  "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/1"},
                 source="greenhouse")
    S.upsert_job(conn, "9", "co", {"id": "9", "title": "AI Engineer",
                                   "slug": "ai"}, source="wellfound")
    conn.commit()
    return cfg


class Pages:
    """Page factory: the browser boundary. Counts how many pages were opened."""
    def __init__(self, fields, challenge=None):
        self.fields, self.challenge, self.opened = fields, challenge, []

    def __call__(self):
        p = FakePage(self.fields, self.challenge)
        self.opened.append(p)
        return p


def _client(tmp_path, profile_dir, pages, monkeypatch, readonly=False):
    if readonly:
        monkeypatch.setenv("JOBSEARCH_READONLY", "1")
    else:
        monkeypatch.delenv("JOBSEARCH_READONLY", raising=False)
    cfg = _cfg(tmp_path, profile_dir)
    helper = B.Assistant(profile_dir, cfg.assist_hosts, new_page=pages)
    return TestClient(create_app(cfg, assistant=helper)), cfg


GH = "greenhouse:acme/1"


def test_assist_fills_records_opened_with_resume_and_inboxes(tmp_path, profile_dir,
                                                             monkeypatch):
    pages = Pages(FORM)
    c, cfg = _client(tmp_path, profile_dir, pages, monkeypatch)
    r = c.post("/api/assist", json={"job_id": GH})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "filled" and body["filled"] >= 3
    assert pages.opened[0].url == "https://job-boards.greenhouse.io/acme/jobs/1"
    assert pages.opened[0].settled == "networkidle"   # waited before filling

    tag = P.current_resume(profile_dir).tag
    conn = S.connect(cfg.db_path)
    ev = conn.execute("SELECT status, resume FROM status_event WHERE source_job_id=?",
                      (GH,)).fetchall()
    assert [tuple(e) for e in ev] == [("opened", tag)]

    # "did you apply?" -> yes carries the resume onto the application
    assert c.post("/api/confirm", json={"job_id": GH, "applied": True}).status_code == 200
    applied = conn.execute("SELECT resume FROM status_event WHERE source_job_id=? "
                           "AND status='applied'", (GH,)).fetchone()[0]
    assert applied == tag

    inbox = {q["question"] for q in P.load_inbox(profile_dir)}
    assert "Do you have a security clearance?" in inbox


def test_resume_tag_survives_sync_and_export_strips_it(tmp_path, profile_dir, monkeypatch):
    c, cfg = _client(tmp_path, profile_dir, Pages(FORM), monkeypatch)
    c.post("/api/assist", json={"job_id": GH})
    conn = S.connect(cfg.db_path)
    carried = S.export_events(conn)
    assert carried[0]["resume"]
    fresh = S.connect(tmp_path / "fresh.db")
    S.import_events(fresh, carried)
    assert fresh.execute("SELECT resume FROM status_event").fetchone()[0] == carried[0]["resume"]
    S.clear_events(conn)
    assert conn.execute("SELECT COUNT(*) FROM status_event WHERE resume IS NOT NULL"
                        ).fetchone()[0] == 0


def test_drift_refuses_before_any_browser_opens(tmp_path, profile_dir, monkeypatch):
    (profile_dir / "cv-new.pdf").write_bytes(b"%PDF-1.4 new cv")
    pages = Pages(FORM)
    c, cfg = _client(tmp_path, profile_dir, pages, monkeypatch)
    r = c.post("/api/assist", json={"job_id": GH})
    assert r.status_code == 409 and r.json()["detail"]["kind"] == "drift"
    assert pages.opened == []
    conn = S.connect(cfg.db_path)
    assert conn.execute("SELECT COUNT(*) FROM status_event").fetchone()[0] == 0


def test_off_allowlist_job_is_refused_without_a_browser(tmp_path, profile_dir, monkeypatch):
    pages = Pages(FORM)
    c, _ = _client(tmp_path, profile_dir, pages, monkeypatch)
    r = c.post("/api/assist", json={"job_id": "wellfound:9"})
    assert r.status_code == 409 and pages.opened == []


def test_route_takes_a_job_id_not_a_url(tmp_path, profile_dir, monkeypatch):
    pages = Pages(FORM)
    c, _ = _client(tmp_path, profile_dir, pages, monkeypatch)
    r = c.post("/api/assist", json={"job_id": "https://evil.example/form"})
    assert r.status_code == 404 and pages.opened == []


def test_challenge_interstitial_fills_nothing(tmp_path, profile_dir, monkeypatch):
    pages = Pages(FORM, challenge={"interstitial": True, "widget": True})
    c, cfg = _client(tmp_path, profile_dir, pages, monkeypatch)
    r = c.post("/api/assist", json={"job_id": GH})
    assert r.json()["status"] == "challenge"
    page = pages.opened[0]
    assert [c_ for c_ in page.calls if c_[0] != "evaluate"] == []
    assert {c_[1] for c_ in page.calls} == {B.CHALLENGE_JS}
    conn = S.connect(cfg.db_path)
    assert conn.execute("SELECT COUNT(*) FROM status_event").fetchone()[0] == 0


def test_a_page_that_asks_nothing_about_you_is_not_filled(tmp_path, profile_dir,
                                                          monkeypatch):
    """A closed posting redirected to a job board: search box and filters only
    (Greenhouse, live 2026-09-19). Nothing typed, nothing inboxed, no event."""
    board = [raw("0", "Search", "text"), raw("1", "Department", "combobox")]
    pages = Pages(board)
    c, cfg = _client(tmp_path, profile_dir, pages, monkeypatch)
    r = c.post("/api/assist", json={"job_id": GH})
    assert r.json()["status"] == "no-form"
    assert [x for x in pages.opened[0].calls if x[0] in B.ALLOWED_PAGE_CALLS] == []
    assert P.load_inbox(profile_dir) == []
    conn = S.connect(cfg.db_path)
    assert conn.execute("SELECT COUNT(*) FROM status_event").fetchone()[0] == 0


def test_second_click_reuses_the_open_tab(tmp_path, profile_dir, monkeypatch):
    """After solving a challenge the user presses Assist again: same tab, no
    second page load."""
    pages = Pages(FORM)
    c, _ = _client(tmp_path, profile_dir, pages, monkeypatch)
    c.post("/api/assist", json={"job_id": GH})
    c.post("/api/assist", json={"job_id": GH})
    assert len(pages.opened) == 1


def test_hosted_readonly_has_no_assist_route(tmp_path, profile_dir, monkeypatch):
    pages = Pages(FORM)
    c, _ = _client(tmp_path, profile_dir, pages, monkeypatch, readonly=True)
    assert c.post("/api/assist", json={"job_id": GH}).status_code in (404, 405)
    assert c.get("/api/meta").json()["assist"] is None
    assert pages.opened == []
