---
status: current
last_verified: 2026-09-19
applies_to: [jobsearch/src/assist, docs/resume.example]
---

# Resume, profile and answers — the runbook

Assisted apply ([ADR-017](../architecture/decisions/ADR-017-assisted-apply.md))
fills application forms from files you keep up to date. This page covers how to
keep them that way. Commands run from `jobsearch/`.

## The folder

`docs/resume/` is **gitignored**; nothing in it ever reaches the public repo. The
path comes from `assist.profile_dir` in `targets.yaml`, and `JOBSEARCH_PROFILE_DIR`
overrides it.

| File | Who writes it | What it is |
|---|---|---|
| `*.pdf` | you | Every resume version you have used. **Keep old ones.** Past applications point at them |
| `profile.yaml` | you (Claude can draft) | Your resume as structured data: name, contact, links, current role, work, education |
| `answers.yaml` | you (Claude can draft) | Screening answers, each with the question wording it answers |
| `resume-pin.yaml` | the tool | Which PDF is current, and the sha256 of every PDF ever pinned |
| `unanswered.yaml` | the tool | Questions met on real forms that no answer matched: your to-do list |

The tool never rewrites `profile.yaml` or `answers.yaml`, so comments in them are
safe. `docs/resume.example/` has the same files with fake data. It is the schema
reference, and the tests run against it.

## First-time setup

```
pip install playwright           # optional dependency; Assist is hidden without it
python -m src.cli profile check  # validates everything, reports drift
python -m src.cli profile pin Resume_2026.pdf
python -m src.cli answers pending
```

Assist uses your installed Chrome with its own profile in
`jobsearch/browser-profile/` (gitignored). If Chrome is not installed, run
`playwright install chromium` once.

## When your resume changes

1. Put the new PDF in `docs/resume/`. Leave the old one there.
2. `python -m src.cli profile check` now reports **DRIFT** (`X.pdf is new and has
   not been pinned`). **Assist refuses to fill until this is resolved.** That is
   deliberate: the new PDF sent beside an old title or employer typed into the
   form is an application that contradicts itself.
3. Update `profile.yaml` to match: current title and company, the new role in
   `work`, new links. The easy way is to ask Claude to "refresh my profile from the
   new resume" and review the diff.
4. `python -m src.cli profile pin <new>.pdf`. It prints what `profile.yaml` says,
   so you vouch for it while looking at it, then records the PDF.

The same drift fires if the current PDF is edited in place (same name, different
bytes) or deleted. Every application sent through Assist records which PDF went
with it (`status_event.resume`, e.g. `Resume_2026.pdf@1a2b3c4d`), so a year later
you can still tell which version a company has.

**Several old PDFs at once?** Pin them oldest first and the current one last.
Pinning never adopts other PDFs silently: if you pinned the old one by mistake, the
new one would still show as drift.

## When forms ask new questions

1. Assist leaves any question it cannot answer **blank and outlined in amber**, and
   adds it to `unanswered.yaml` with its exact wording, its options, the ATS and
   the jobs it appeared on.
2. `python -m src.cli answers pending` lists those questions, most frequent first,
   together with answers you left blank and answers past their review date.
3. Add an entry to `answers.yaml`:

   ```yaml
   - id: notice_how_much
     match:
       - How much (timeline) notice do you need to give your current job?
     answer: 30 days
     reviewed: 2026-09-19
     expires_days: 60
   ```

   Copy the question text from `unanswered.yaml` into `match:`. It clears from the
   inbox on the next `answers pending` or Assist run.

### Matching rules

These are strict on purpose. A blank field costs you ten seconds; a wrong answer
can cost you the job.

- The question is normalised: lowercased, accents and punctuation removed, and a
  trailing `*`, `✱` or `(required)` dropped. It must then **equal** a `match:`
  entry or **contain** a `contains:` entry. Nothing is fuzzy: "the UK" never
  matches "the US".
- **Two entries matching one question means neither is used.** The field is left
  blank as `ambiguous`; tighten one of the two entries.
- **Dropdowns and radios:** the answer must be one of the offered options. When a
  form words its options differently, list its wording under `options:`:
  `{"No": ["No, I will not require sponsorship"]}`. An answer that is not among
  the options is left blank (`no-option`). Example: "2 weeks" against a Yes/No
  notice-period question.
- **Multi-line boxes** are only filled from entries marked `free_text: true`.
- **Volatile answers** (salary, notice period, current CTC) take `expires_days`.
  Once `reviewed` is older than that, the answer is **not filled** until you
  update `reviewed:`. A number from six months ago is not sent by accident.
- Standard fields (name, email, phone, LinkedIn, GitHub, website, location, current
  company and title) come from `profile.yaml` and need no answer entry. The label
  printed on the form decides which one a field is; an answer entry with the same
  question wording overrides it.
- YAML: write each question as a `- ` list item on its own line. Inside `[...]`, a
  `?` breaks parsing. Quote any text containing `": "`.

## What you see in the browser

A black banner at the top gives the counts. Every field Assist touched is outlined:

- **green**: typed by the tool. Check it.
- **blue**: a dropdown or button group the tool cannot pick in, because it never
  clicks. Your answer is shown under it; pick that option.
- **amber**: yours to fill. The note under it says why (not in answers.yaml, blank
  in profile.yaml, stale, and so on).

Uploads are green with a note to **check the file shows as attached**. Uploads were
verified on every ATS on 2026-09-19. Greenhouse only accepts a file once its page has
fully loaded, which is why Assist waits for the page to settle before it fills
anything.
If a CAPTCHA is on the page, the banner says so; solve it yourself before
submitting. If the whole page is a challenge ("Just a moment"), nothing is filled.
Solve it, then press Assist again: the same tab is reused and filled.

Then **you** press Submit and answer "did you apply?" in the tray.

## Checking the extractor against live forms

`python ../scripts/check_assist.py --pick` opens the newest job per ATS with the
**fake** example profile (a valid fake PDF). It prints every field left for you,
with the reason, and checks that each upload shows its file name with no uploader
error. It exits non-zero on a missing upload or a page with no form.
Use it when a form fills badly or an ATS changes its markup. It does not write to
`jobs.db`.
