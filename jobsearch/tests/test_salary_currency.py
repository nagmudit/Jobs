"""Salary is only comparable once the currency is known.

The bug this exists for: `salary_min` mixed currencies in one sortable column
with `salary_currency` left NULL for every Wellfound row. Measured on the live
corpus 2026-09-06 -- 2,331 rows, no currency on any of them:

    ₹30L – ₹47L   -> salary_min 3000000   currency NULL
    $200k – $275k -> salary_min  200000   currency NULL

Sorting descending therefore put every Indian salary above every US one, on the
primary sort control in the UI. The symbol was being parsed by `_NUM` and then
thrown away.
"""

from __future__ import annotations

import pytest

from src import derive as D
from src import store as S


# --- the currency itself ------------------------------------------------------


@pytest.mark.parametrize("raw,want", [
    ("$114k – $173k", "USD"),
    ("₹30L – ₹47L", "INR"),
    ("€60k – €90k", "EUR"),
    ("£62k – £168k", "GBP"),
    ("₹45L – ₹60L • 0.1% – 0.3%", "INR"),
    # No symbol at all: unknown, never assumed to be dollars.
    ("50,000 - 70,000", None),
    ("", None),
    (None, None),
])
def test_currency_is_read_from_the_symbol(raw, want):
    assert D.salary_currency(raw) == want


def test_an_unknown_symbol_is_not_guessed():
    """Unparsed beats wrongly parsed: a currency we do not have a rate for must
    not silently become USD."""
    assert D.salary_currency("¥8,000,000") is None


# --- the comparable value -----------------------------------------------------


def test_usd_conversion_makes_the_two_comparable():
    """The whole point: ₹30L is about $36k, so it must sort BELOW $200k."""
    inr = D.salary_usd(3_000_000, "INR")
    usd = D.salary_usd(200_000, "USD")
    assert inr is not None and usd is not None
    assert inr < usd, "a 30L INR salary still outranks $200k"
    assert 30_000 < inr < 45_000, f"₹30L converted to ${inr}, which is not plausible"


def test_usd_of_a_usd_amount_is_unchanged():
    assert D.salary_usd(200_000, "USD") == 200_000


def test_unknown_currency_yields_no_comparable_value():
    """A number with no currency cannot be compared. NULL keeps it out of the
    cross-currency sort rather than pretending it is dollars."""
    assert D.salary_usd(500_000, None) is None
    assert D.salary_usd(500_000, "XYZ") is None
    assert D.salary_usd(None, "USD") is None


# --- through the view ---------------------------------------------------------


@pytest.fixture
def conn(tmp_path):
    return S.connect(tmp_path / "t.db")


def _wf(conn, jid, title, comp):
    S.upsert_company(conn, "acme", {"name": "Acme"}, source="wellfound")
    S.upsert_job(conn, jid, "acme",
                 {"title": title, "slug": "s", "compensation": comp},
                 source="wellfound")
    conn.commit()


def test_the_view_derives_currency_for_wellfound_rows(conn):
    """Wellfound states no currency field; it is recoverable from the symbol in
    `compensation`, which is where it was being dropped."""
    _wf(conn, "1", "IN role", "₹30L – ₹47L")
    _wf(conn, "2", "US role", "$200k – $275k")
    got = {r["title"]: (r["salary_currency"], r["salary_min"], r["salary_usd_min"])
           for r in conn.execute(
               "SELECT title, salary_currency, salary_min, salary_usd_min FROM jobs")}
    assert got["IN role"][0] == "INR" and got["IN role"][1] == 3_000_000
    assert got["US role"][0] == "USD" and got["US role"][1] == 200_000


def test_sorting_by_usd_puts_them_in_the_right_order(conn):
    """The regression itself, asserted end to end through the view."""
    _wf(conn, "1", "IN role", "₹30L – ₹47L")
    _wf(conn, "2", "US role", "$200k – $275k")
    order = [r["title"] for r in conn.execute(
        "SELECT title FROM jobs WHERE salary_usd_min IS NOT NULL "
        "ORDER BY salary_usd_min DESC")]
    assert order == ["US role", "IN role"], \
        "native-value sort still ranks a 30L INR salary above $200k"


def test_a_source_that_states_its_currency_keeps_it(conn):
    """Himalayas and Ashby publish a currency column. A derived guess must never
    override what the source actually said."""
    S.upsert_company(conn, "h", {"name": "H"}, source="himalayas")
    S.upsert_job(conn, "9", "h", {
        "title": "EU role", "guid": "9", "minSalary": 60000, "maxSalary": 90000,
        "currency": "EUR", "salaryPeriod": "annual",
        "pubDate": "2026-09-01T00:00:00Z", "applicationLink": "https://x.test",
    }, source="himalayas")
    conn.commit()
    r = conn.execute("SELECT salary_currency, salary_usd_min FROM jobs "
                     "WHERE title='EU role'").fetchone()
    assert r["salary_currency"] == "EUR"
    assert r["salary_usd_min"] is not None


def test_a_non_annual_period_has_no_comparable_value(conn):
    """An hourly 45 must not become a $45 'annual' salary in the sort."""
    S.upsert_company(conn, "h", {"name": "H"}, source="himalayas")
    S.upsert_job(conn, "8", "h", {
        "title": "hourly", "guid": "8", "minSalary": 45, "maxSalary": 60,
        "currency": "USD", "salaryPeriod": "hourly",
        "pubDate": "2026-09-01T00:00:00Z", "applicationLink": "https://x.test",
    }, source="himalayas")
    conn.commit()
    r = conn.execute("SELECT salary_min, salary_usd_min FROM jobs "
                     "WHERE title='hourly'").fetchone()
    assert r["salary_min"] is None and r["salary_usd_min"] is None


def test_the_raw_string_survives_alongside_every_derivation(conn):
    """Unparsed beats wrongly parsed, and the original must always be readable
    so a wrong conversion is auditable rather than invisible."""
    _wf(conn, "1", "IN role", "₹30L – ₹47L")
    r = conn.execute("SELECT salary_raw FROM jobs").fetchone()
    assert r["salary_raw"] == "₹30L – ₹47L"
