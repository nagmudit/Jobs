---
status: active
created: 2026-09-19
last_updated: 2026-09-19
areas: [jobsearch/src/assist, jobsearch/src/web, jobsearch/src/store.py, jobsearch/src/cli.py, docs/resume.example]
---

# Assisted apply

## Objective

Cut an ATS application to a review plus one click. The tool opens the form in a
visible browser and fills it from the user's profile and screening answers; **the user
reviews, solves any CAPTCHA, and submits**. Decided in
[ADR-017](../../architecture/decisions/ADR-017-assisted-apply.md). The request included
bypassing Cloudflare and other CAPTCHAs; that part was declined and is recorded there.

A requirement from the start: **the resume will change, and the answers will grow.**
Both have a designed path. See
[resume-and-answers.md](../../engineering/resume-and-answers.md).

## What was built

- `src/assist/profile.py`: loads and validates `profile.yaml` and `answers.yaml`
  (unknown keys raise). Resume **pin and drift** use sha256, never file dates. Also
  holds the `unanswered.yaml` inbox and stale-answer detection.
- `src/assist/match.py`: pure mapping from field descriptors to a fill plan. Matching
  is exact after normalisation; two matching answers count as ambiguous and are
  left blank; choice answers must be an offered option; free text needs an opt-in;
  stale answers are not filled; the visible label beats the input name.
- `src/assist/browser.py`: `form_url` (exact host allowlist; Greenhouse company-site
  jobs go to Greenhouse's embed form), extraction and marking scripts, and
  `apply_plan`, which is limited to fill, select, check and attach. The `Assistant`
  worker thread owns one headed Chrome.
- `status_event.resume`, added through `ADD_COLUMNS`. It is set on the `opened`
  event that Assist records and carried onto `applied` at confirm. Export clears it
  with the rest of the log, and sync carries it.
- `/api/assist` (via `mutating()`, so absent on the hosted deployment),
  `/api/meta.assist`, and an **Assist** button on allowlisted rows.
- `config.py` `assist:` section, validated at load (bare hostnames only).
- CLI: `profile check`, `profile pin <pdf>`, `answers pending`.
- `docs/resume.example/`: fake-data templates, which are also the test fixtures.
- `scripts/check_assist.py`: a manual live check using the fake profile.
- Gitignored: `docs/resume/` (the PDF sat untracked but *not ignored* in this public
  repo when the work began) and `jobsearch/browser-profile/`.
- The user's real `docs/resume/profile.yaml` was structured from their PDF, with a
  seeded `answers.yaml` of 17 common questions (**all answers left blank for the
  user**), and the PDF pinned. None of it is in git.

## Found by the live check, 2026-09-19

`check_assist.py` was run with the fake profile against one Greenhouse form, one
Greenhouse embed (a company-site job), one Ashby form and one Lever form. It found six
defects that the offline suite could not have found. All are fixed:

1. **Wrong answer:** Ashby labels its `_systemfield_name` input "First Name", and the
   name hint typed "Ada Example" into it. The label now wins, and a test pins it.
2. Greenhouse react-select's hidden "Select..." validation inputs were extracted as
   fields. `aria-hidden` and `tabindex=-1` controls are now skipped.
3. Lever radio groups took the option text ("Yes") as the question, and Ashby's took
   the first statement. The question search now starts at the group's shared
   container.
4. Lever custom questions were labelled by placeholder ("Type your response"). The
   placeholder is now the last resort.
5. File inputs were labelled "Attach" and went to the inbox. They now use the section
   heading, and file fields never go to the inbox (answers.yaml cannot supply a file).
6. Answer notes were squeezed inside narrow dropdowns. They are now placed at the
   first ancestor at least 240px wide.

Verified afterwards: every extracted question on all three ATSs carries its real
wording, and the Ashby screenshot shows First Name "Ada" and Last Name "Example".

## Second live check, 2026-09-19: every ATS host, and the resume upload

The fake profile was run against **9 forms covering every allowlisted host and every
route a job reaches one by**: Greenhouse via `job-boards.greenhouse.io`,
`boards.greenhouse.io` and the embed form (company-site job); Ashby; Lever; Workable;
and vickybytes listings that link to Greenhouse, Ashby and Lever. `check_assist.py`
now uses a **valid** one-page fake PDF and checks each upload itself: after
attaching, the file name must appear on the page and no uploader error may appear.

**Greenhouse upload: the cause was timing, not the file.** A diagnostic attached
the same valid PDF two ways. Immediately after the form rendered, Greenhouse
had not yet fetched its upload credentials (`presigned_fields`), and its uploader
threw `reading 'uploadFile'`. After the page went network-quiet, the file was
POSTed to Greenhouse's S3 bucket and `ada.pdf` showed. Fixes: Assist waits for
`networkidle` (capped at 15 s) before filling, and attaches files last. Both are
tested, and both tests were verified failable.

Found and fixed in the same pass:

7. **Workable's resume was never attached.** Labels were read with `textContent`,
   which includes an icon's SVG fallback ("SVGs not supported by this browser.").
   Labels now use rendered `innerText`, and a file input takes its label from the
   first `aria-labelledby` id ("Resume"), not the description after it.
8. **A closed posting was treated as a form.** Greenhouse redirected a closed job to
   the company's board, and Assist filled nothing but logged "Search" and
   "Department" to the inbox. A page with no resume upload and no profile field
   is now reported as `no-form`: nothing is touched, logged or recorded. Tested.
9. Workable's leading `*` required marker was left on question text.

Final run on the final code:

| Form | Filled | Resume upload |
|---|---|---|
| Greenhouse, job-boards (iSpot) | 7 | shown on page |
| Greenhouse, boards.greenhouse.io (Neuralink) | 6 | shown on page |
| Greenhouse, embed for a company-site job (Netskope) | 7 | shown on page |
| vickybytes -> Greenhouse (Speechify) | `no-form`: posting closed, redirected to the board | n/a |
| Ashby (Everis) | 6 | shown on page |
| vickybytes -> Ashby (Nanonets) | 6 | shown on page |
| Lever (Samba TV) | 8 | shown on page (Lever upper-cases it) |
| vickybytes -> Lever (Enveda) | 8 | shown on page |
| Workable (Sweep360) | 5 | shown on page |

## Verification

- `python -m pytest tests -q`: **503 passed**. The 3 new files hold 79 tests:
  `test_assist_profile.py`, `test_assist_match.py`, `test_assist_browser.py`.
- Nine guards were mutated and each turned the named test red: never clicks,
  headed only, drift refuses, allowlist is the last word, resume carried to
  applied, ambiguous is not picked, new PDF is drift, label beats input name,
  stale answer not sent.
- The only fake is the browser page, at the Playwright boundary. The profile,
  matcher, store and route run for real.

## Remaining

- Uploads are verified with a fake PDF on every ATS, but not with the user's real
  one. The "check it shows as uploaded" note stays: an uploader that changes will
  fail visibly on the page, not in our code.
- **Custom widgets are not filled.** Greenhouse's comboboxes (country, location,
  education, most screening questions) and Ashby's location get the answer shown
  beside them for the human to pick. Filling them would mean clicking, which is
  what keeps "never submits" checkable. Revisit only with a design that keeps that
  property testable.
- **Not run through the real web UI end to end.** The route is covered by
  `TestClient` tests and the browser path by `check_assist.py`, but nobody has
  clicked Assist in the served page. The wider actions column (240px) is not
  checked by `scripts/check_layout.py`.
- **Not run with the user's real profile.** A permission check blocked copying
  personal files into scratch space, and typing real details into a live form
  belongs to the user. Their first real Assist click is that test.
- Workable was tested on the corpus's only Workable job, so its coverage is one form.
- `answers.yaml` has 17 blank entries. They are the user's to fill.

## Done when

- [x] Profile, answers, pin and drift, inbox, with the lifecycle documented
- [x] Strict matcher, verified failable
- [x] Browser path limited to fill, select, check and attach, headed, no stealth, allowlisted
- [x] Resume version recorded per application, never published
- [x] Live-checked on Greenhouse, the Greenhouse embed, Ashby and Lever
- [x] Every ATS host live-checked, with uploads verified on the page (fake PDF)
- [ ] Used for real applications for a week; then decide on custom widgets
