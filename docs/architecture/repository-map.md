---
status: current
last_verified: 2026-09-03
applies_to: [jobsearch/src, wellfound-probe/src]
---

# Repository Map

## Shape

Two independent Python projects side by side, no shared package, no monorepo tooling.
They are separated by *lifecycle*, not by layer:

- **`wellfound-probe/`** — a finished feasibility study. It answered "can we read
  Wellfound, and how" and produced `REPORT.md`. It is evidence, kept because
  re-deriving it costs a day of live requests. Do not extend it.
- **`jobsearch/`** — the tool built on that answer. All active work happens here.

`jobsearch/` is a layered pipeline with one hard rule: **every byte from the network
enters through `src/fetch.py`, and every byte stored stays raw until the SQL view
derives it.**

```
targets.yaml → config → crawl ─┬→ fetch ──→ (network, rate-limited, robots-gated)
                               └→ parse ──→ store (raw JSON)
                                              ↓
                                      jobs VIEW ← derive.py (SQL functions)
                                              ↓
                                     web/app.py → static/index.html
```

## Directories

### `jobsearch/src/`

**Holds:** the whole tool. Flat module layout — 9 modules, no sub-packages except `web`.
**Entry:** `python -m src.cli`

| Module | Holds | Depends on |
|---|---|---|
| `config.py` | `targets.yaml` loading, CLI overrides, the delay-floor guard | nothing internal |
| `fetch.py` | **The only network egress.** robots, rate limit, disk cache, cf logging | `assertions` |
| `parse.py` | `__NEXT_DATA__` → Apollo → raw nodes. No normalisation | `assertions` |
| `assertions.py` | The four corpus-integrity guards | nothing internal |
| `store.py` | SQLite schema, writers, and the `jobs` view | `derive` |
| `derive.py` | salary/equity/size parsing, registered as SQLite functions | nothing internal |
| `crawl.py` | Slice loop, telemetry, resume | `fetch`, `parse`, `store`, `assertions` |
| `enrich.py` | Detail pages: JSON-LD + rendered comp | `fetch`, `store` |
| `cli.py` | Argument parsing and command dispatch | everything |
| `web/app.py` | FastAPI; all filtering in SQL | `store`, `enrich`, `fetch` |
| `web/static/index.html` | The entire UI. Vanilla JS, no build step | — |

**Rules:**
- Nothing but `fetch.py` opens an HTTP connection. Unenforced by tooling — this is
  convention only, and a real risk (see `docs/plans/active/`).
- `derive.py` must import nothing internal; it is loaded during `store.connect()`
  before the schema exists.
- `parse.py` never normalises. It returns raw dicts; shaping happens in SQL.

### `jobsearch/tests/`

Offline only. `fixtures.py` builds synthetic `__NEXT_DATA__` pages reproducing each
failure shape, so the assertions can be tested without touching Wellfound.

### `wellfound-probe/`

Six probe modules (P1–P6) behind `python -m src.run --probe <id>`, plus `REPORT.md`.
`results/*.json` holds the raw measurements behind every number in the report.

## Where things live

| Looking for | Go to |
|---|---|
| Roles/locations being crawled | `jobsearch/targets.yaml` |
| Rate limiting, robots, cache | `jobsearch/src/fetch.py` |
| Why a crawl stopped | `slice_stats.ended_reason`, or `src/crawl.py` |
| A new filterable column | `store.py` `VIEWS` + maybe `derive.py`. **Not** a re-crawl |
| Filter SQL | `web/app.py::_where` |
| UI markup, JS, styling | `web/static/index.html` (single file) |
| Why we read Wellfound this way | `wellfound-probe/REPORT.md`, ADR-001 |
| Silent-failure evidence | `wellfound-probe/REPORT.md` §5 |

## Boundaries

| Rule | Enforced? |
|---|---|
| All egress via `Fetcher.get` | **Convention only** — no test, no lint |
| `delay_range` floor ≥ 3.0 s | Enforced in `config.py`, raises at load |
| robots.txt honoured | Enforced in `fetch.py`, raises `RobotsDisallowed` |
| Halt on mitigation | Enforced in `assertions.check_mitigation` |
| No role/location literals in `src/` | **Convention only** |

The two "convention only" rows are the highest-value automation targets in the repo.

## Surprises

Things that will mislead you if nobody says them:

- **`wellfound-probe/` looks like live code and is not.** It has its own `src/`,
  `cache/`, and CLI. It is frozen. `jobsearch/` does not import from it.
- **The `jobs` view calls Python.** `salary_min`, `salary_max`, `equity_max`,
  `size_label`, `size_min`, `remote_label` are Python functions registered on the
  connection in `store.connect()`. Open the DB with plain `sqlite3` on the command
  line and **every query against `jobs` fails with "no such function"**. Use
  `store.connect()`.
- **`location: remote` is not a location.** It maps to `/role/r/{role}`, a different
  page type whose GraphQL args carry `remote: true` instead of a location member.
  Handled in `fetch.search_url`.
- **Slicing widens results, it does not narrow them.** Overlapping slices are
  deliberate; each gets its own page budget. See ADR-004.
- **`page_wrap` is a normal, successful slice ending**, not an error, despite being
  raised as an exception. `crawl_slice` catches it and closes the slice cleanly.
- `wellfound-probe/` and `jobsearch/` both have a `src/` package. Running `python -m
  src.cli` from the wrong directory silently targets the wrong project.
