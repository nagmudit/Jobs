#!/usr/bin/env python3
"""Render the UI in a real browser and measure it.

Source review cannot see a layout. Every one of the bugs below shipped past a
careful reading of the CSS and was caught the first time the page was actually
rendered and measured:

  * `display:none` on the sidebar made <section> the first grid item, so it
    landed in the now-0px first track and the whole table collapsed to a sliver.
  * `<colgroup>` widths beat th/td widths in a fixed-layout table, so every
    column width rule written on `th:nth-child()` was dead and the Apply button
    rendered clipped to "Ap...".
  * `overflow-x:auto` with the other axis `visible` computes to `auto` too, which
    silently makes the element a scroll container -- and a `position:sticky`
    header cannot escape one. The column heads sat 40px low and stopped sticking.
  * `select{width:100%}`, meant for the sidebar, also hit the toolbar selects and
    forced their labels onto a second line, doubling the toolbar's height.

NOT part of `pytest tests`, deliberately: the suite is offline and dependency-free
by contract (AGENTS.md), while this needs a running server, playwright, and a
browser binary. It is a tool to reach for when changing the page, not a gate.

    pip install playwright            # into a scratch venv, not jobsearch/
    playwright install chromium       # or point CHROME_EXE at an existing build

    cd jobsearch && python -m src.cli serve --port 8099
    python scripts/check_layout.py

Env: APP_URL (default http://127.0.0.1:8099/), CHROME_EXE, SHOT_DIR.
Exit code is non-zero if any check fails, so it can gate a commit by hand.
"""
from __future__ import annotations

import os
import sys

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("playwright is not installed. See this file's docstring.")

URL = os.environ.get("APP_URL", "http://127.0.0.1:8099/")
CHROME = os.environ.get("CHROME_EXE")
OUT = os.environ.get("SHOT_DIR", "layout-shots")
SIZES = [(1440, 900), (1100, 800), (900, 800), (700, 800), (390, 844)]

fails: list[str] = []


def launch(pw):
    return pw.chromium.launch(executable_path=CHROME) if CHROME else pw.chromium.launch()


def check_layout(browser) -> None:
    """Every viewport, both sidebar states."""
    print("LAYOUT")
    for w, h in SIZES:
        page = browser.new_page(viewport={"width": w, "height": h})
        page.goto(URL, wait_until="networkidle")
        for state in ("open", "collapsed"):
            want = "on" if state == "open" else "off"
            # Drive to the state; never assume the starting one. Below 900px the
            # page starts collapsed, so a blind click measures the opposite.
            if page.evaluate("() => document.documentElement.dataset.aside") != want:
                page.click("#aside-toggle")
                page.wait_for_timeout(120)
            m = page.evaluate("""() => {
              const sec = document.querySelector('main > section');
              const de = document.documentElement;
              const apply = document.querySelector('.sbtn.apply');
              const th = document.querySelector('#grid thead th');
              const tbl = document.querySelector('#grid');
              return {
                overflow: de.scrollWidth - de.clientWidth,
                vw: de.clientWidth,
                section: Math.round(sec.getBoundingClientRect().width),
                // Two ways the actions can break, and they need different
                // checks: the button shrinking (what produced "Ap..."), and --
                // once flex:none stops that -- the buttons overflowing the cell
                // instead. Measuring only the button missed the second entirely.
                applyClipped: apply ? apply.scrollWidth > apply.clientWidth + 1 : false,
                actsOverflow: (() => {
                  const cell = document.querySelector('td.acts');
                  if (!cell) return 0;
                  const btns = [...cell.querySelectorAll('.sbtn')];
                  if (!btns.length) return 0;
                  const right = Math.max(...btns.map(b => b.getBoundingClientRect().right));
                  return Math.round(right - cell.getBoundingClientRect().right);
                })(),
                headerGap: th && tbl
                  ? Math.round(th.getBoundingClientRect().top
                               - tbl.getBoundingClientRect().top) : 0,
                cols: [...document.querySelectorAll('#grid thead th')]
                        .filter(t => getComputedStyle(t).display !== 'none').length,
              };
            }""")
            bad = []
            if m["overflow"] > 1:
                bad.append(f"page overflows by {m['overflow']}px")
            if m["section"] < m["vw"] * 0.45:
                bad.append(f"content column only {m['section']} of {m['vw']}px")
            if m["applyClipped"]:
                bad.append("Apply button clipped")
            if m["actsOverflow"] > 1:
                bad.append(f"row actions overflow their cell by {m['actsOverflow']}px")
            if m["headerGap"] != 0:
                bad.append(f"{m['headerGap']}px dead band above the column heads")
            fails.extend(f"{w}px/{state}: {b}" for b in bad)
            print(f"  {'ok  ' if not bad else 'FAIL'} {w:>5}x{h} {state:<9} "
                  f"content {m['section']:>5}px  cols {m['cols']:>2}")
            os.makedirs(OUT, exist_ok=True)
            page.screenshot(path=f"{OUT}/{w}-{state}.png")
        page.close()


def check_sticky(browser) -> None:
    """The column heads must stay put while rows scroll under them."""
    print("\nSTICKY HEADER")
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.goto(URL, wait_until="networkidle")
    page.evaluate("() => document.querySelector('.tablewrap').scrollTop = 1200")
    page.wait_for_timeout(120)
    m = page.evaluate("""() => {
      const th = document.querySelector('#grid thead th');
      const wrap = document.querySelector('.tablewrap');
      return {thTop: Math.round(th.getBoundingClientRect().top),
              wrapTop: Math.round(wrap.getBoundingClientRect().top),
              scrolled: Math.round(wrap.scrollTop)};
    }""")
    ok = m["thTop"] == m["wrapTop"] and m["scrolled"] > 0
    if not ok:
        fails.append(f"header did not stick (th {m['thTop']} vs pane {m['wrapTop']})")
    print(f"  {'ok  ' if ok else 'FAIL'} scrolled {m['scrolled']}px, "
          f"header held at {m['thTop']}")
    page.close()


def check_theme(browser) -> None:
    """Three states, persistence, and the OS-dark case."""
    print("\nTHEME")
    ctx = browser.new_context(viewport={"width": 1200, "height": 800},
                              color_scheme="light")
    page = ctx.new_page()
    page.goto(URL, wait_until="networkidle")

    def snap():
        return page.evaluate("""() => ({
          label: document.querySelector('#theme span')?.textContent,
          bg: getComputedStyle(document.body).backgroundColor})""")

    seen = []
    for _ in range(4):
        seen.append(snap())
        page.click("#theme")
        page.wait_for_timeout(100)
    order = [s["label"] for s in seen]
    if order != ["System", "Light", "Dark", "System"]:
        fails.append(f"theme cycle order is {order}")
    if seen[1]["bg"] == seen[2]["bg"]:
        fails.append("Light and Dark render the same background")
    print(f"  {'ok  ' if order == ['System','Light','Dark','System'] else 'FAIL'} "
          f"cycle {' -> '.join(order)}")

    page.click("#theme")
    page.wait_for_timeout(100)
    before = snap()
    page.reload(wait_until="networkidle")
    after = snap()
    kept = before["label"] == after["label"] and before["bg"] == after["bg"]
    if not kept:
        fails.append("theme did not survive a reload")
    print(f"  {'ok  ' if kept else 'FAIL'} {before['label']} survived a reload")
    ctx.close()

    ctx = browser.new_context(viewport={"width": 1200, "height": 800},
                              color_scheme="dark")
    page = ctx.new_page()
    page.goto(URL, wait_until="networkidle")
    d = page.evaluate("() => getComputedStyle(document.body).backgroundColor")
    follows = d == seen[2]["bg"]
    if not follows:
        fails.append(f"system-dark {d} != explicit-dark {seen[2]['bg']}")
    print(f"  {'ok  ' if follows else 'FAIL'} System follows an OS set to dark")
    ctx.close()


def main() -> int:
    with sync_playwright() as pw:
        b = launch(pw)
        check_layout(b)
        check_sticky(b)
        check_theme(b)
        b.close()
    print()
    if fails:
        print("FAILURES:")
        for f in fails:
            print("  -", f)
        return 1
    print(f"all checks passed; screenshots in {OUT}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
