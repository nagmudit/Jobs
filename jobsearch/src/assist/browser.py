"""The browser side of assisted apply: open one form, fill it, never submit.

Everything here that touches a page is deliberately narrow (ADR-017):

- The page API used is `fill`, `select_option`, `check`, `set_input_files`, and
  `evaluate` with one of the scripts in `PAGE_SCRIPTS`. There is no click, no
  key press, no navigation after the first `goto`, and no `form.submit()`.
  `tests/test_assist_browser.py` drives `apply_plan` against a fake page and
  fails on any other call, and scans the scripts for click/submit/synthetic
  events.
- Headed, always. `HEADLESS` is a constant, not a parameter.
- No stealth of any kind: the user's installed Chrome, its own UA, no
  `navigator.webdriver` patch, no init scripts.
- A challenge interstitial means hands off; a challenge widget inside the form
  is never touched. The human deals with both.

Playwright is imported lazily and is optional: without it, `available()` is
False and the UI keeps its plain Apply link.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import queue
import re
import threading
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from . import match as M
from . import profile as P

ROOT = Path(__file__).resolve().parents[2]
BROWSER_PROFILE = ROOT / "browser-profile"      # gitignored: holds cookies

SETTLE_MS = 15_000  # longest wait for a form's page to go network-quiet

HEADLESS = False    # ADR-017: the human watches every form. Never a parameter.

# Page API calls the filler may make. The fake-page test enforces this set.
ALLOWED_PAGE_CALLS = frozenset({"fill", "select_option", "check", "set_input_files"})


class AssistUnavailable(Exception):
    """Assist cannot run here: not configured, Playwright missing, or the job's
    form is not on an allowlisted host."""


# --------------------------------------------------------------------------- #
# Which page to open -- pure, tested offline
# --------------------------------------------------------------------------- #

ATS_BY_HOST = {
    "job-boards.greenhouse.io": "greenhouse",
    "boards.greenhouse.io": "greenhouse",
    "jobs.ashbyhq.com": "ashby",
    "jobs.lever.co": "lever",
    "apply.workable.com": "workable",
}
_GH_ID = re.compile(r"^greenhouse:([A-Za-z0-9_-]+)/(\d+)$")


@dataclass
class Target:
    url: str
    ats: str


def form_url(apply_url: str | None, job_id: str, hosts: list[str]) -> Target | None:
    """The application FORM for a job, on an allowlisted host, or None.

    Each ATS puts its form one step from the posting: Ashby at
    `/application`, Lever and Workable at `/apply`. Greenhouse jobs whose
    apply_url is a company careers page (they embed Greenhouse's form) are
    served from Greenhouse's own embed URL, built from the board token and
    job id we already store -- the same form, on Greenhouse's host.

    The result's host is re-checked against `hosts` EXACTLY, whatever path
    produced it. That check is the allowlist; everything above is convenience.
    """
    url = (apply_url or "").strip()
    host = (urlsplit(url).hostname or "").lower()
    if host not in ATS_BY_HOST:
        m = _GH_ID.match(job_id or "")
        if not m:
            return None
        url = (f"https://job-boards.greenhouse.io/embed/job_app"
               f"?for={m.group(1)}&token={m.group(2)}")
        host = "job-boards.greenhouse.io"
    ats = ATS_BY_HOST[host]
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    if ats == "ashby" and not path.endswith("/application"):
        path += "/application"
    elif ats in ("lever", "workable") and not path.endswith("/apply"):
        path += "/apply"
    out = parts._replace(scheme="https", path=path, fragment="").geturl()
    if (urlsplit(out).hostname or "").lower() not in hosts:
        return None
    return Target(out, ats)


# Profile keys by the input's own name/id, per ATS. Seen on the live forms.
HINT_BY_NAME = {
    # Greenhouse
    "first_name": "first_name", "last_name": "last_name", "email": "email",
    "phone": "phone", "resume": "resume",
    # Lever
    "name": "full_name", "org": "current_company", "location": "location",
    "urls[linkedin]": "linkedin", "urls[github]": "github",
    "urls[portfolio]": "portfolio", "urls[twitter]": "twitter",
    # Ashby
    "_systemfield_name": "full_name", "_systemfield_email": "email",
    "_systemfield_phone": "phone", "_systemfield_location": "location",
    "_systemfield_resume": "resume",
    # Workable
    "firstname": "first_name", "lastname": "last_name",
}


# --------------------------------------------------------------------------- #
# Page scripts. Read-only except MARK_JS, which only adds outlines and notes.
# tests/test_assist_browser.py scans every one for anything that could act.
# --------------------------------------------------------------------------- #

CHALLENGE_JS = r"""
() => {
  const re = /challenges\.cloudflare\.com|hcaptcha\.com|recaptcha|turnstile/i;
  const frames = [...document.querySelectorAll('iframe')].filter(f => re.test(f.src || ''));
  const visible = frames.filter(f => { const r = f.getBoundingClientRect();
                                       return r.width > 30 && r.height > 30; });
  const fields = document.querySelectorAll(
    'input:not([type=hidden]), textarea, select').length;
  const title = /just a moment|attention required|verify you are human/i
    .test(document.title || '');
  const cfPage = !!document.querySelector('#challenge-form, #cf-challenge-running');
  return { interstitial: title || cfPage || (frames.length > 0 && fields === 0),
           widget: visible.length > 0 };
}
"""

EXTRACT_JS = r"""
() => {
  // Required markers at either end: "Name *", "Name ✱" (Lever), "* Name" (Workable).
  const clean = s => (s || '').replace(/\s+/g, ' ')
    .replace(/^[\s*✱]+|[\s*✱]+$/g, '').trim();
  // What a human reads. textContent also returns SVG fallback text ("SVGs not
  // supported by this browser." -- Workable, 2026-09-19); innerText does not.
  const txt = el => (el && (el.innerText ?? el.textContent)) || '';
  window.__jsRefN = window.__jsRefN || 0;
  const ref = el => { if (!el.dataset.jsRef) el.dataset.jsRef = String(window.__jsRefN++);
                      return el.dataset.jsRef; };
  const shown = el => el.getClientRects().length > 0 &&
                      getComputedStyle(el).visibility !== 'hidden';
  const byIds = ids => clean((ids || '').split(/\s+/).map(
                  i => txt(document.getElementById(i))).join(' '));
  const ownLabel = el => {
    if (el.getAttribute('aria-labelledby')) {
      const t = byIds(el.getAttribute('aria-labelledby')); if (t) return t; }
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
                 if (l && clean(txt(l))) return clean(txt(l)); }
    const wrap = el.closest('label'); if (wrap && clean(txt(wrap)))
      return clean(txt(wrap));
    return clean(el.getAttribute('aria-label'));
  };
  const common = els => { let c = els[0].parentElement;
                          while (c && !els.every(e => c.contains(e))) c = c.parentElement;
                          return c || document.body; };
  // The question a group of controls answers: a legend, a labelled group, or
  // the nearest ancestor's first text-bearing child that holds none of them.
  const question = (els) => {
    const first = els[0];
    const fs = first.closest('fieldset');
    if (fs) { const lg = fs.querySelector('legend'); if (lg && clean(txt(lg)))
                return clean(txt(lg)); }
    const grp = first.closest('[role=radiogroup],[role=group]');
    if (grp && grp.getAttribute('aria-labelledby')) {
      const t = byIds(grp.getAttribute('aria-labelledby')); if (t) return t; }
    if (grp && clean(grp.getAttribute('aria-label'))) return clean(grp.getAttribute('aria-label'));
    // Start OUTSIDE the options: above the group's shared container, or
    // above a lone control's own <label>. Otherwise an option's text ("Yes",
    // or Ashby's first statement) is taken for the question (2026-09-19).
    let a = els.length > 1 ? common(els)
                           : (first.closest('label') || first).parentElement;
    for (let i = 0; a && i < 6; i++, a = a.parentElement) {
      for (const c of a.children) {
        if (els.some(e => c.contains(e))) continue;
        if (c.matches('button, input, select, textarea')) continue;
        const t = clean(txt(c)); if (t) return t;
      }
    }
    return '';
  };
  const required = (el, label) => !!(el.required ||
    el.getAttribute('aria-required') === 'true' || /\*\s*$/.test(label || ''));

  const out = []; const groups = {};
  for (const el of document.querySelectorAll('input, textarea, select')) {
    const t = (el.type || '').toLowerCase();
    if (['hidden', 'submit', 'button', 'reset', 'image', 'password'].includes(t)) continue;
    if (el.disabled || el.readOnly) continue;
    if (t !== 'file' && !shown(el)) continue;           // honeypots stay untouched
    // Widget internals: react-select's hidden "Select..." validation input.
    if (t !== 'file' && (el.getAttribute('aria-hidden') === 'true' ||
                         el.tabIndex === -1)) continue;
    const name = (el.name || el.id || '').toLowerCase();
    if (t === 'radio' || t === 'checkbox') {
      const k = t + ':' + (el.name || ref(el));
      (groups[k] = groups[k] || { t, els: [] }).els.push(el); continue;
    }
    let type = el.tagName === 'TEXTAREA' ? 'textarea'
             : el.tagName === 'SELECT' ? (el.multiple ? 'multiselect' : 'select')
             : t === 'file' ? 'file'
             : ['email', 'tel', 'url', 'number'].includes(t) ? t : 'text';
    if (type === 'text' && (el.getAttribute('role') === 'combobox' ||
        el.getAttribute('aria-autocomplete') === 'list')) type = 'combobox';
    // A file input's first aria-labelledby id is its label ("Resume"); later
    // ids are descriptions ("Choose file or drag and drop here" -- Workable).
    const byId = (el.getAttribute('aria-labelledby') || '').split(/\s+/)[0];
    const firstBy = byId ? clean(txt(document.getElementById(byId))) : '';
    const label = type === 'file'
      ? (firstBy || question([el]) || ownLabel(el))
      : (ownLabel(el) || question([el]) || clean(el.getAttribute('placeholder')));
    const options = el.tagName === 'SELECT'
      ? [...el.options].filter(o => o.value !== '' && !o.disabled)
          .map(o => clean(txt(o))) : [];
    out.push({ ref: ref(el), label, type, name, options,
               required: required(el, label), option_refs: {} });
  }
  for (const k in groups) {
    const { t, els } = groups[k];
    if (t === 'checkbox' && els.length === 1) {
      const el = els[0]; const label = ownLabel(el) || question(els);
      out.push({ ref: ref(el), label, type: 'checkbox',
                 name: (el.name || '').toLowerCase(), options: [],
                 required: required(el, label), option_refs: {} });
      continue;
    }
    const option_refs = {}; const options = [];
    for (const el of els) { const o = ownLabel(el) || clean(el.value);
                            options.push(o); option_refs[o] = ref(el); }
    const label = question(els);
    // The group's ref is its shared container, so the outline and note wrap
    // every option; options are ticked through their own refs.
    out.push({ ref: ref(common(els)), label, type: t === 'radio' ? 'radio' : 'checkboxes',
               name: (els[0].name || '').toLowerCase(), options,
               required: els.some(e => required(e, label)), option_refs });
  }
  // Button groups (Ashby's Yes/No): a fieldset or labelled group holding only
  // type=button buttons. Extracted so the answer can be SHOWN beside them.
  for (const g of document.querySelectorAll('fieldset, [role=radiogroup], [role=group]')) {
    if (g.querySelector('input, select, textarea')) continue;
    const bs = [...g.querySelectorAll('button[type=button]')].filter(shown);
    if (bs.length < 2 || bs.length > 8) continue;
    const label = question(bs);
    if (!label) continue;
    out.push({ ref: ref(g), label, type: 'buttons', name: '',
               options: bs.map(b => clean(txt(b))), required: false,
               option_refs: {} });
  }
  return out;
}
"""

MARK_JS = r"""
([ref, kind, text]) => {
  const el = document.querySelector(`[data-js-ref="${ref}"]`);
  if (!el) return false;
  document.querySelectorAll(`[data-js-note="${ref}"]`).forEach(n => n.remove());
  const colour = { filled: '#1a7f37', hint: '#1d6feb', blank: '#b35900' }[kind];
  // Narrow or hidden controls (a dropdown's inner input, a styled file input)
  // are marked at the first ancestor wide enough to hold a readable note.
  let box = el;
  while (box.parentElement && box.parentElement !== document.body &&
         box.getBoundingClientRect().width < 240) box = box.parentElement;
  box.style.outline = `2px solid ${colour}`;
  box.style.outlineOffset = '2px';
  if (text) {
    const n = document.createElement('div');
    n.setAttribute('data-js-note', ref);
    n.textContent = text;
    n.style.cssText = `font:12px/1.4 system-ui,sans-serif;color:${colour};margin:4px 0`;
    box.insertAdjacentElement('afterend', n);
  }
  return true;
}
"""

BANNER_JS = r"""
(text) => {
  let b = document.querySelector('[data-js-banner]');
  if (!b) { b = document.createElement('div'); b.setAttribute('data-js-banner', '1');
            document.body.insertAdjacentElement('afterbegin', b); }
  b.textContent = text;
  b.style.cssText = 'position:sticky;top:0;z-index:2147483647;padding:8px 12px;' +
    'background:#111;color:#fff;font:13px/1.4 system-ui,sans-serif';
  return true;
}
"""

PAGE_SCRIPTS = (CHALLENGE_JS, EXTRACT_JS, MARK_JS, BANNER_JS)


# --------------------------------------------------------------------------- #
# Extract + apply. `page` is anything with Playwright's sync Page surface.
# --------------------------------------------------------------------------- #

def extract(page) -> tuple[list[M.Field], dict[str, dict[str, str]]]:
    fields, option_refs = [], {}
    for raw in page.evaluate(EXTRACT_JS):
        hint = HINT_BY_NAME.get(raw.get("name") or "")
        if hint == "resume" and raw["type"] != "file":
            hint = None
        fields.append(M.Field(ref=raw["ref"], label=raw.get("label") or "",
                              type=raw["type"], required=bool(raw.get("required")),
                              options=list(raw.get("options") or []), hint=hint))
        option_refs[raw["ref"]] = dict(raw.get("option_refs") or {})
    return fields, option_refs


NOTES = {
    "unmatched": "Not in answers.yaml yet -- logged to unanswered.yaml",
    "blank": "answers.yaml has this question with no answer yet",
    "ambiguous": "Two answers.yaml entries match this -- tighten one",
    "no-option": "Your answer is not one of these options",
    "stale": "Your answer is past its review date -- check it",
    "free-text": "Long answer: write it yourself (or set free_text: true)",
    "missing-profile": "Blank in profile.yaml",
    "cover-letter": "Cover letter: attach it yourself if you want one",
    "upload": "Attach this yourself if you want to",
    "unsupported": "Fill this one yourself",
}


def _sel(ref: str) -> str:
    return f'[data-js-ref="{ref}"]'


def apply_plan(page, fills: list[M.Fill], option_refs: dict[str, dict[str, str]]) -> None:
    """Carry out a fill plan. The ONLY place that changes a form's values.

    Every action is a value change on one control. Nothing here can press a
    button, and the test that pins ALLOWED_PAGE_CALLS keeps it that way.
    """
    # Uploads last: every other field is typed first, which gives an uploader
    # that initialises late (Greenhouse) the most time before it is used.
    for f in sorted(fills, key=lambda f: f.action == "upload"):
        ref, src = f.field.ref, f.source or ""
        try:
            if f.action == "fill":
                page.locator(_sel(ref)).fill(f.value)
            elif f.action == "upload":
                page.locator(_sel(ref)).set_input_files(f.value)
            elif f.action == "select" and f.field.type in ("select", "multiselect"):
                page.locator(_sel(ref)).select_option(label=f.value)
            elif f.action == "select":        # radio / checkbox group: tick the option
                for v in (f.value if isinstance(f.value, list) else [f.value]):
                    page.locator(_sel(option_refs[ref][v])).check()
            elif f.action == "check":
                page.locator(_sel(ref)).check()
        except Exception as e:                # a control that refused a value
            f.action, f.reason = "skip", "unsupported"
            src = f"{src} ({type(e).__name__})"
        if f.action == "hint":
            page.evaluate(MARK_JS, [ref, "hint", f"Your answer: {f.value}"])
        elif f.action == "skip":
            page.evaluate(MARK_JS, [ref, "blank", NOTES.get(f.reason or "", f.reason)])
        elif f.action == "upload":
            # Some uploaders reject a file set this way (seen on Greenhouse,
            # 2026-09-19). The page is the only judge; ask the human to look.
            page.evaluate(MARK_JS, [ref, "filled",
                                    "Attached your resume -- check it shows as uploaded"])
        else:
            page.evaluate(MARK_JS, [ref, "filled", ""])


def looks_like_application(fills: list[M.Fill]) -> bool:
    """An application asks who you are: a resume upload or a profile field
    (name, email, ...). A page with neither is something else."""
    return any(f.source == "resume" or (f.source or "").startswith("profile:")
               for f in fills)


def summarise(fills: list[M.Fill], widget: bool) -> dict:
    done = [f for f in fills if f.action in ("fill", "upload", "select", "check")]
    left = [f for f in fills if f.action in ("skip", "hint")]
    required_left = [f for f in left if f.field.required]
    msg = (f"jobsearch filled {len(done)} field(s). Review EVERYTHING -- green was "
           f"typed by the tool, blue shows your answer to pick, amber is yours "
           f"to fill ({len(required_left)} required). Then submit it yourself.")
    if widget:
        msg += " This form has a CAPTCHA: solve it yourself before submitting."
    return {
        "filled": len(done),
        "left": [{"label": f.field.label, "type": f.field.type,
                  "required": f.field.required, "reason": f.reason or f.action,
                  "hint": f.value if f.action == "hint" else None} for f in left],
        "required_left": len(required_left),
        "widget": widget,
        "banner": msg,
    }


# --------------------------------------------------------------------------- #
# The worker: one thread owns the browser (sync Playwright is not thread-safe)
# --------------------------------------------------------------------------- #

def available() -> bool:
    return importlib.util.find_spec("playwright") is not None


class Assistant:
    """Owns one visible browser. `assist()` is called from a web request and
    blocks until that one form has been filled.

    Each call is one user click on one job. There is no batch entry point, no
    background loop and no retry: a failure is reported, and trying again is
    the user clicking again.
    """

    def __init__(self, profile_dir: Path, hosts: list[str],
                 browser_dir: Path = BROWSER_PROFILE, new_page=None):
        self.profile_dir = profile_dir
        self.hosts = list(hosts)
        self.browser_dir = browser_dir
        # The browser boundary. None = a real, visible Chrome via Playwright;
        # tests pass a factory of fake pages, which is the only fake they use.
        self._new_page = new_page or self._playwright_page
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._pw = self._ctx = None
        self._pages: dict[str, object] = {}

    # -- request thread ----------------------------------------------------
    def prepare(self, job_id: str, apply_url: str | None) -> tuple[Target, P.Resume]:
        """Every refusal that needs no browser, raised before one is opened."""
        if self._new_page == self._playwright_page and not available():
            raise AssistUnavailable(
                "Playwright is not installed: pip install playwright "
                "(see docs/engineering/resume-and-answers.md)")
        target = form_url(apply_url, job_id, self.hosts)
        if target is None:
            raise AssistUnavailable(
                "this job's form is not on an allowlisted ATS host (assist.hosts)")
        P.load_profile(self.profile_dir)
        P.load_answers(self.profile_dir)
        return target, P.current_resume(self.profile_dir)   # ProfileDrift raises

    def assist(self, job_id: str, target: Target, resume: P.Resume,
               timeout: float = 180.0) -> dict:
        return self._on_worker(lambda: self._handle(job_id, target, resume), timeout)

    def screenshot(self, job_id: str, path: Path) -> bool:
        """Capture the job's open tab (scripts/check_assist.py). Read-only."""
        def shot():
            page = self._pages.get(job_id)
            if page is None or page.is_closed():
                return False
            page.screenshot(path=str(path), full_page=True)
            return True
        return self._on_worker(shot, 60.0)

    def page_text(self, job_id: str) -> str | None:
        """The open tab's visible text (scripts/check_assist.py). Read-only."""
        def read():
            page = self._pages.get(job_id)
            if page is None or page.is_closed():
                return None
            return page.inner_text("body")
        return self._on_worker(read, 60.0)

    def _on_worker(self, fn, timeout: float):
        fut: Future = Future()
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="assist",
                                                daemon=True)
                self._thread.start()
        self._q.put((fn, fut))
        return fut.result(timeout)

    # -- worker thread -----------------------------------------------------
    def _run(self) -> None:
        while True:
            fn, fut = self._q.get()
            try:
                fut.set_result(fn())
            except Exception as e:            # reported to the user, not retried
                fut.set_exception(e)

    def _context(self):
        if self._ctx is not None:
            return self._ctx
        from playwright.sync_api import Error, sync_playwright

        if self._pw is None:
            self._pw = sync_playwright().start()
        self.browser_dir.mkdir(parents=True, exist_ok=True)
        kw = dict(user_data_dir=str(self.browser_dir), headless=HEADLESS,
                  no_viewport=True)
        try:        # the user's installed Chrome: a real browser, not a disguise
            self._ctx = self._pw.chromium.launch_persistent_context(channel="chrome", **kw)
        except Error:
            self._ctx = self._pw.chromium.launch_persistent_context(**kw)
        self._ctx.on("close", lambda *_: self._forget())
        self._pages = {}
        return self._ctx

    def _forget(self) -> None:
        self._ctx, self._pages = None, {}

    def _playwright_page(self):
        try:
            return self._context().new_page()
        except Exception:                     # window closed by the user: relaunch
            self._forget()
            return self._context().new_page()

    def _handle(self, job_id: str, target: Target, resume: P.Resume) -> dict:
        page = self._pages.get(job_id)
        if page is None or page.is_closed():
            page = self._new_page()
            page.goto(target.url, wait_until="domcontentloaded", timeout=45_000)
            self._pages[job_id] = page
            try:
                page.wait_for_selector("input:not([type=hidden]), textarea, select",
                                       timeout=15_000)
            except Exception:
                pass                          # judged below: challenge or no form
            # Let the page finish loading before touching it. Greenhouse fetches
            # its upload credentials (`presigned_fields`) after the form renders;
            # a file attached before that crashed its uploader ("reading
            # 'uploadFile'") while one attached after went to S3 (2026-09-19).
            # Capped: analytics keep some pages from ever going quiet.
            try:
                page.wait_for_load_state("networkidle", timeout=SETTLE_MS)
            except Exception:
                pass
        page.bring_to_front()

        ch = page.evaluate(CHALLENGE_JS)
        if ch.get("interstitial"):
            return {"status": "challenge", "url": target.url,
                    "message": "The site is showing a challenge. Solve it in the "
                               "browser window, then press Assist again."}

        profile = P.load_profile(self.profile_dir)
        answers = P.load_answers(self.profile_dir)
        fields, option_refs = extract(page)
        if not fields:
            return {"status": "no-form", "url": target.url,
                    "message": "No application form found on the page."}
        fills = M.plan(fields, profile, answers, resume.path, dt.date.today())
        if not looks_like_application(fills):
            # A closed posting often redirects to the company's job board: its
            # search box and filters are fields, but not an application
            # (Greenhouse, 2026-09-19). Touch nothing and log nothing.
            return {"status": "no-form", "url": target.url,
                    "message": "No application form here -- the posting may be "
                               "closed. Check the page in the browser window."}
        apply_plan(page, fills, option_refs)
        P.record_unanswered(
            self.profile_dir,
            [{"label": f.field.label, "type": f.field.type,
              "options": f.field.options, "reason": f.reason}
             for f in fills if f.inbox],
            job_id, target.ats)
        out = summarise(fills, bool(ch.get("widget")))
        page.evaluate(BANNER_JS, out["banner"])
        return {"status": "filled", "url": target.url, "ats": target.ats,
                "resume": resume.tag, **out}
