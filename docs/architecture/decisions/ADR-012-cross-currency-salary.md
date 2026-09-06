# ADR-012: Salary sorts on an approximate USD value, and displays the native one

**Status:** accepted · **Date:** 2026-09-06

## Context

`salary_min` / `salary_max` held a bare number with no currency attached. On the live
corpus, 2026-09-06:

| `salary_currency` | rows |
|---|---|
| NULL | **2,331** |
| USD / CAD / EUR / GBP / SEK / PLN | 257 |

Wellfound publishes no currency field, so every Wellfound row landed in the NULL bucket
even though the symbol was right there in the string. `derive._NUM` was already capturing
it as a named group and then discarding it.

The result was that the UI's primary sort was wrong:

    ₹1.2 cr – ₹1.3 cr   ->  salary_min 12000000   (~$144k)
    $520k – $600k       ->  salary_min   520000

Sorting `salary_min DESC` put every Indian salary above every American one, because the
numbers were bigger. The same flaw applied to the `salary_min` **filter**: a threshold of
1,500,000 matched every INR job and excluded a $200k US one.

## Decision

**Recover the currency, sort on a USD-normalised value, and display the native figure.**

Three separate things, deliberately kept separate:

1. **`salary_currency`** — derived from the symbol (`$ ₹ € £`) when the source does not
   state one. Purely factual, no estimation. A source that *does* publish a currency
   (Himalayas, Ashby) always wins; the derivation only fills a gap.
2. **`salary_usd_min` / `salary_usd_max`** — the native figure converted with a rate
   table in `src/derive.py`. **For ordering only.**
3. **Display stays native.** The UI shows `₹30L – ₹47L`, because that is what the
   employer actually said. The USD value appears only as a tooltip, labelled
   *"approx, for sorting"*.

Showing `$36k` where the posting says `₹30L` would be a fabrication. The sort needs a
common scale; the reader does not.

### The rate table is a snapshot, and that is a real cost

`_FX_TO_USD` is hardcoded with a stated `as of` date. It will drift. That is accepted
because the alternative — a live FX feed — adds a second network dependency, a failure
mode, and a third party to be polite to, all to refine an ordering that is approximate by
nature. A salary range is itself a range; a 5% rate drift does not change whether ₹30L
ranks below $200k.

Because the derivations are SQL functions rather than stored columns (ADR-002), editing
the table re-derives every row on the next query, with no migration and no re-crawl.

### Unknown currency yields NULL, not a default

`salary_usd(amount, None)` returns NULL rather than assuming dollars. A figure whose
currency is unknown stays *out* of the cross-currency sort, sorting last under
`NULLS LAST`. Defaulting to USD is precisely the bug being replaced: it would make an
uncomparable number look comparable. Thirteen rows in the corpus are in this state.

The filter is the one place that falls back to the native figure
(`COALESCE(salary_usd_max, salary_max)`), because dropping those rows would hide them
from a threshold they might well satisfy — a filter that silently omits rows is worse
than one that occasionally includes a borderline case.

## Consequences

- `jobs` gains two columns. The view is now wrapped in an outer `SELECT` so the USD keys
  can be computed from `salary_min`/`salary_currency` after those are themselves derived.
  `jobs_core` is untouched, so the positional `CORE_COLUMNS` contract is unaffected.
- 2,331 rows gained a currency with no re-crawl: 1,905 USD, 594 INR, 46 EUR, 19 GBP.
- Cross-currency comparison is now *approximate but ordered*, where before it was
  *precise-looking and wrong*.
- Salary figures shown to the user are unchanged and remain exact.

## Alternatives rejected

**Sort within currency only** (group by currency, sort inside each). Honest, but it makes
"the best-paying jobs" unanswerable across a corpus that is deliberately multi-country.

**Store a converted value at ingest.** Freezes the rate into the data and needs a
migration whenever it changes. Deriving keeps ADR-002's property that a better derivation
costs nothing.

**A live FX API.** A second network dependency and another third party to rate-limit,
for precision the use case does not need.

## Verification

`tests/test_salary_currency.py`. The load-bearing one asserts the ordering end to end
through the view — `$200k` above `₹30L` — and it was watched failing first. Mutation:
removing the currency derivation from the view reddens it, and defaulting an unknown
currency to a 1.0 rate reddens the NULL test.

## Related

`src/derive.py::salary_currency` · `src/derive.py::salary_usd` · `src/store.py` CREATE_OUTER_VIEW ·
`src/web/app.py::SORTS` · ADR-002
