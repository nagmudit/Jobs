# ADR-017: Assisted apply — the tool pre-fills, the user submits

**Status:** accepted · **Date:** 2026-09-19 · **Amends** "no auto-apply, by design" in
`AGENTS.md`, and the "all network access goes through `Fetcher.get`" conduct rule

## Context

The tool finds jobs; applying is still a copy-paste job per posting. About 440 jobs in
the corpus link to a real ATS form (Ashby 208, Greenhouse 180, Lever 21, Workable 1),
and those forms ask the same dozen things every time.

We were asked for *automatic* applying, including a way past Cloudflare and other
CAPTCHAs. The CAPTCHA part is refused and stays refused: a challenge is a site owner
saying "a human, past this point". Defeating one is evasion, the conduct rules already
forbid it, and for job applications the cost lands on the user. ATS vendors flag
bot-submitted applications, and a flag can quietly end the user's candidacy at a company.

Separately, about 5,800 jobs (Wellfound, Himalayas) link to platform pages that need
the user's logged-in account on that platform. Scripting a logged-in account is the
fastest way to lose it.

## Decision

**Assisted apply.** A click on **Assist** opens that job's `apply_url` in a visible
browser window. The tool fills what it knows from the user's structured profile and
screening answers, attaches the resume PDF, and outlines everything it left blank.
**The user reviews, solves any challenge, and presses Submit.** The tool never does.

The browser is a second path to the network outside `Fetcher.get`. That is acceptable
here, and only here, because the traffic is one page load a human asked for, in a real
browser the human is watching. That is the same traffic as clicking the link. It is
not a crawl, so the crawl rules (3–5 s spacing, robots.txt, the per-host halt) are not
the right shape for it. These rules replace them, **each enforced in code and tested**:

1. **One user click, one page.** `/api/assist` takes a job id, not a URL. The URL
   comes from that job's `apply_url`. There is no batch endpoint, no queue, no
   background loop and no retry.
2. **Host allowlist.** `assist.hosts` in `targets.yaml`: `job-boards.greenhouse.io`,
   `boards.greenhouse.io`, `jobs.ashbyhq.com`, `jobs.lever.co` and `apply.workable.com`.
   Any other host, including Wellfound, Himalayas and company career sites, keeps a
   plain Apply link. Exact host match, never a suffix or pattern.
3. **Never submits, never clicks.** The code that touches the page is limited to
   fill, select, check, attach a file, and marking fields with an outline and a
   note. It has no click, no key press and no `form.submit()`. A test drives it
   against a fake page and fails on any other call, and a second test scans the
   page scripts it injects for click, submit and synthetic events. Custom
   widgets that can only be answered by clicking, such as Greenhouse's searchable
   dropdowns and Ashby's Yes/No buttons, get the user's answer shown next to them
   as a note, for the human to pick.
4. **Headed only, no stealth.** The browser is launched with `headless=False`, fixed
   in code. It is the user's installed Chrome (`channel="chrome"`) with its own UA,
   with no `navigator.webdriver` patch, no fingerprint change and no proxy. It uses a
   dedicated persistent profile in `jobsearch/browser-profile/` (gitignored, since it
   holds cookies).
5. **A challenge is the human's, always.** If the page is a challenge
   *interstitial* (Cloudflare "Just a moment", a Turnstile or CAPTCHA page with no
   application form), the tool fills nothing and reports "solve it in the window,
   then press Assist again". A widget *inside* the form (Greenhouse loads an
   invisible reCAPTCHA on every form, and it runs when the human presses Submit)
   is never touched, and neither is any element inside a challenge frame. The
   form's own fields are still filled, and the user is told to solve the widget
   before submitting. No solver service, ever.
6. **Local only.** The route is registered through `mutating()`, so the hosted
   read-only deployment does not have it at all.
7. **Profile drift refuses.** If the resume PDF on disk is not the one the profile was
   pinned to, Assist raises instead of filling. A new PDF sent next to old typed fields
   (an old title, an old employer) is a plausible-but-wrong application, the same class
   of silent failure ADR-003 raises on.

## Personal data

Everything personal lives in `docs/resume/`, which is gitignored. That covers resume
PDFs, `profile.yaml`, `answers.yaml`, and the tool-written `unanswered.yaml`. The
committed `docs/resume.example/` holds the same files with fake data, so the schema is
public and tested while the data never is. Which resume version went to which job is
recorded in `status_event.resume`. That column is cleared with the rest of the log by
`export`, so it never reaches the published corpus.

## Consequences

- An ATS application drops to a review plus one click.
- `AGENTS.md`'s "no auto-apply" becomes "pre-fill only, the user submits".
- Filling is strict: exact normalised question match, and an answer must be one of
  the offered options. Anything else is left blank. More blanks is the intended price
  of never typing a wrong answer into an application.
- Playwright becomes an **optional** runtime dependency. Without it, Assist reports
  itself unavailable and everything else works.
- CAPTCHA-heavy postings are no worse than today: the user solves the challenge, as
  they would by hand.

## Alternatives

- **Fully automatic submit.** Rejected: an unreviewed application is irreversible and
  public to the employer, and mass submission is what ATS bot detection exists for.
- **CAPTCHA solver or stealth browser.** Rejected; see Context and the conduct rules.
- **ATS "apply" APIs.** Greenhouse's and Lever's application endpoints need the
  *employer's* API key. They are not available to a candidate.
- **Fuzzy question matching or LLM-written answers.** Deferred. Both produce plausible
  wrong answers with nobody noticing; the unanswered inbox makes gaps visible instead.

## Related

`jobsearch/src/assist/` · `jobsearch/src/web/app.py` (`/api/assist`) ·
`jobsearch/targets.yaml` (`assist:`) ·
[resume & answers runbook](../../engineering/resume-and-answers.md) ·
[plan](../../plans/active/assisted-apply.md)
