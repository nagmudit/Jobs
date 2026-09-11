---
status: active
created: 2026-09-09
last_updated: 2026-09-12
areas: [jobsearch/src/store.py, jobsearch/src/cli.py, jobsearch/src/web]
---

# Application analytics

## Objective

Know what was applied to and when, how many this week, and how those applications
turned out. **Small on purpose** — a handful of numbers, no charts.

## The problem it solves

`user_state` holds one **mutable** row per job. `set_status` overwrote `status`
and `updated_at` in place, so marking a job `rejected` on the 20th destroyed the
fact that it was applied to on the 1st. "What did I apply to last week" was
unanswerable, not merely unimplemented.

## What changed

**`status_event`** — an append-only log of every status change. `set_status`
remains the single writer of both tables: it upserts `user_state` exactly as
before, so every existing filter, facet and view is untouched, and appends an
event. Everything else here is a query over that.

**Three statuses added**: `interviewing`, `offer`, `rejected`, giving the funnel
`shortlisted → applied → interviewing → offer | rejected`. Rounds go in `note`;
there is no per-round tracking by design.

**`SCHEMA_VERSION` 2 → 3**, with a `migrate()` step seeding one event per
existing mark from its `updated_at`, guarded on absence so reconnects do not
duplicate.

## The four consequences that carried the real risk

1. **`export` clears the log.** The corpus is published to a public repo daily; a
   dated record of every job applied to must never leave the machine. Verified on
   the real corpus with plain `sqlite3`: 0 events in the published file.
2. **`sync` carries the log across.** Unlike a mark, an event cannot be recreated
   by clicking the button again.
3. **`INVESTED_STATUSES` gained `interviewing` and `offer`**, so `prune` and
   `carry_forward_marked` never drop a job under active interview.
4. **`import_user_state` replays with `record=False`.** Found by a failing test:
   replaying marks through `set_status` appended a *second* set of events dated
   today, so every sync silently re-dated the history it was meant to preserve.

## Verification

- `python -m pytest tests -q` → **333 passed**, three consecutive runs.
- `tests/test_application_analytics.py`, 19 tests. Five mutants each caught by the
  right test: history not recorded, export not stripping, sync not carrying,
  counting `user_state` instead of events, and `interviewing` dropped from
  `INVESTED_STATUSES`.
- End-to-end on a copy of the real 6,342-job corpus: applied date survived a later
  rejection, published copy clean, sync restored all 8 events.

**A flaky test caught before it shipped.** `median_days_to_reply` used
`CAST(... AS INTEGER)`, which truncates: a reply after 9.99999 days reported 9.
The test passed alone and failed in the full suite, because the gap depended on
how long the run took. The metric now rounds (a reply after 1.9 days is 2, not 1)
and the test pins both timestamps instead of one.

## Deliberately not built

Charts, per-round tracking, reminders or follow-up nudges, goals, CSV export,
company-level CRM. If a number needs a chart to be read, it is the wrong number
for this tool.

## 2026-09-11 — the capture problem, and the page

Marking status per row was never going to be used: it needs a trip back and a
hunt for the row. **The Apply click is now the trigger.** It records `opened`
(not `applied` — opening is not applying), and a tray at the top asks "did you
apply?" so the row never has to be found. Answering yes dates the application at
the **open** time, so confirming on Thursday something opened on Monday does not
move it into the wrong week. Applications with no outcome after
`followup_days` (10) get "any news?" in the same tray; "Not yet" snoozes.

`opened`, `skipped` and `nudged` are event-only — never in `user_state`, so every
filter, facet and the funnel are untouched. `/api/event` refuses funnel statuses
so an application can never be logged without also setting current state.

Row controls went from six to three (Apply · Shortlist · Hide); outcomes are
answered in the tray where the context already is.

**The page**, against the repo's `ui-ux-pro-max` skill:

- Emoji controls (☎ 🏆 ✖, added 2026-09-09) replaced with an inline SVG sprite —
  the skill's priority-4 anti-pattern. Zero emoji remain.
- First focus rings on the page; `prefers-reduced-motion`; `scroll-margin-top` so
  the sticky header cannot hide a focused row (WCAG 2.2 focus-not-obscured).
- Body 13px → 14px/1.5; `--dim` raised to clear 4.5:1 in both themes.
- **A real bug fixed:** `aside` and `th` hardcoded `top:41px`. Adding the stats
  row on 2026-09-09 made the header taller, so the sticky column heads had been
  sitting *under* it since. Now measured into `--hdr` on load and resize.
- Stat values use proportional figures, not `tabular-nums` — per the `dataviz`
  skill, equal-width digits make a standalone number look loose; tabular is for
  columns that align.

Not taken from the skill, deliberately: its `--design-system` pattern was
"Enterprise Gateway" (mega menu, client logos, Contact Sales) — a B2B landing
pattern with nothing to do with a single-user tool. Its Google-Fonts suggestion
was dropped too; this page is served locally and from a cold-start function.

### 2026-09-11 (later) — neobrutalist revamp

The incremental fixes were not enough; the page needed a visual direction, not
more patches. Taken from the `ui-ux-pro-max` style record for `neubrutalism`
rather than invented: `--bw:3px`, `--shadow:4px 4px 0`, no gradients, no blur,
sharp-ish corners, heavy type, mechanical press (`active:translate(2px,2px)` with
the shadow removed).

**Minimal, as asked:** one accent (yellow) carries the style; `--good`/`--bad`/
`--warn` appear only where they mean something. Chunk goes on containers and
controls — the table keeps 1px row rules, because 3px borders on fifty rows is a
wall, not a style.

Scrollbars are styled in both engines (`scrollbar-color` and
`::-webkit-scrollbar`), thumb in accent with an ink border.

**Contrast was computed, not eyeballed** — a script checks all 28 text/background
pairs the stylesheet actually uses, in both themes. It caught two real failures:

- the light-mode green behind its cream label was **4.07:1**, under the 4.5 floor
  (now `#157347`, 5.50:1);
- the dark-mode focus ring `#6AA6FF` scored **1.79:1 against the yellow accent**,
  so focus on the Apply and tray buttons — the two controls that matter most —
  was effectively invisible. One focus colour (`#1D6FEB`) now serves both themes
  and clears 3:1 against panel, paper and accent in each.

Deviations from the style record, deliberate: system font stack rather than Outfit
via CDN (local page, cold-start function — a render-blocking font request buys
nothing), and a 4px radius rather than 0, which reads modern rather than raw.

### 2026-09-11 (later still) — glassmorphism, and a theme switch

The neobrutalist pass was rejected on looks. Replaced with the skill's
`glassmorphism` record: `--blur:16px` (its 10–20px band), 1px light borders,
layered depth, soft shadows, over a three-blob colour mesh — glass needs
something behind it or the effect does not read.

**Its accessibility risk is rated "conditional", and this is why.** A translucent
surface has no colour of its own; text contrast depends on whatever the mesh is
doing underneath. The style suggests 15% opacity, which cannot carry dense body
text. Surfaces that hold text run at **42–90%** instead, and a script composites
each one over *every extreme of the mesh* and measures ink and muted text against
the result — 12 surface/theme combinations, all passing, worst case 5.36:1.
Legibility over effect, which is what the style's own checklist asks for.

`backdrop-filter` is on a handful of large surfaces only, **never on table rows**
— one blurred surface per row repaints the whole table every scroll frame.
Opaque fallbacks for `@supports not (backdrop-filter)` and for
`prefers-reduced-transparency`.

**Theme switch** — three states, not two: system / light / dark, cycling from a
header button, persisted in `localStorage`. "System" is a real state (no
attribute, so the media query decides) and keeps tracking the OS if it flips
while the page is open. The explicit-dark and system-dark token blocks are
asserted identical, so the two paths cannot drift.

### 2026-09-11 (from a screenshot) — monochrome, one-row chrome, collapsible filters

First look at the thing rendered. Three problems visible that source review had
not surfaced:

- **The chrome owned ~150px** — title row, then a full row spent on the sentence
  "No applications tracked yet", then a band of dead space with the theme button
  floating in it. Now **one 40px row**: filters toggle, brand, stat chips,
  application figures, theme. The strip scrolls sideways rather than wrapping, so
  chrome height is fixed no matter how many stats appear, and `#appstats:empty`
  renders nothing at all — an empty state does not deserve a row.
- **Apply was truncated to "Ap…"**. The global `td{overflow:hidden;
  text-overflow:ellipsis}` was clipping the actions cell. Actions now opt out and
  the column has a fixed 154px width; every column got an explicit width, so the
  two empty ones (Size, Equity) stop taking space from Title and Company.
- **`Bengaluru, India |`** — this feed ends locations with a dangling separator.
  Left alone the pipe renders into the table and the dirty string becomes its own
  location facet beside the clean one. 20 rows in the corpus; fixed in the view,
  so re-deriving cleaned them with no re-crawl (ADR-002).

**Palette is now monochrome.** Gradients gone entirely, glass gone with them, and
every neutral token is a true grey — a channel-spread check caught `--dim` and
the borders carrying a 6-8 point blue cast, which is exactly the tint the brief
objected to. Colour survives in two places only: semantic status, and nothing
else — even the focus ring is full-contrast ink, and the primary button is the
ink inverted rather than a brand hue. 32 contrast pairs checked across both
themes, all passing.

**Filters collapse.** On a table of thousands of rows the rows are the product;
a 288px filter panel is a tool, not furniture. The toggle persists, and collapsed
means `display:none` so nothing inside stays tabbable. Under 900px it starts
collapsed and opens as an overlay.

### 2026-09-12 — rendered it, and found four bugs reading could not

The collapse broke the page outright: `display:none` on the sidebar made
`<section>` the **first** grid item, so it landed in the now-0px first track.
Setting `--aside-w:0` was never enough — the template itself has to become
single-column.

That prompted actually rendering the page in Chromium and measuring it, which
immediately found three more, all of which had survived careful source review:

- **`<colgroup>` beats `th`/`td` width** in a fixed-layout table. Every column
  width written on `th:nth-child()` was dead; Actions computed to 123px against
  146px of buttons, which is what clipped Apply to "Ap...". Widths moved onto the
  `<col>` elements, one elastic Title column absorbing the rest.
- **`overflow-x:auto` with the other axis `visible` computes to `auto`**, so the
  new `.tablewrap` silently became a scroll container — and a sticky header
  cannot escape one. The column heads sat 40px low and stopped sticking. The
  table now owns its scroll deliberately: chrome and sidebar hold still, only
  rows move, header sticks at `top:0` of its own pane.
- **`select{width:100%}`**, meant for the sidebar, also hit the toolbar selects,
  pushing each label onto its own line — a 67px toolbar for two dropdowns, now
  47px.

Also: the overlay sidebar below 900px covered Title and Company with no way out
but the button it came from. It has a scrim and Escape now.

**`scripts/check_layout.py`** keeps the capability: five viewports × two sidebar
states, sticky-header behaviour, and the full theme cycle including persistence
and the OS-dark case. Deliberately **not** in `pytest tests` — the suite is
offline and dependency-free by contract, and this needs a server, playwright and
a browser. Verified failable against three mutants; the third exposed a weak
assertion (measuring the button caught it shrinking but not the buttons
overflowing their cell), which is now checked separately.

## Remaining

- Numbers are whole-corpus; they do not respect the active filters. Deliberate for
  now — "how many did I apply to this week" should not change when a facet is
  clicked. Revisit if it turns out to be wanted.
- No per-source response *rate* (only application counts by source). Needs more
  data before it would mean anything.
- **Screen-reader behaviour is unverified.** Layout, sticky behaviour and the
  theme control are now measured in a real browser; contrast is computed. Nothing
  has been driven with an actual screen reader.
- The tray polls only on load and after an answer. If the page is left open for
  days, a follow-up that becomes due will not appear until a reload.

## Done when

- [x] Applied date survives an outcome recorded later
- [x] 7d / 30d / all-time counts, funnel, response rate, median time to reply
- [x] History never published
- [x] History survives a corpus refresh
- [x] Suite green, every guard verified failable
- [ ] Used for a week, then decide whether any number is missing or noise
