#!/usr/bin/env python3
"""Run Assist against real application forms and report what it extracted.

The offline suite proves what Assist DECIDES; only a real form shows what it
SEES. ATS markup changes without notice, so this is the tool to reach for when
a form fills badly, or before trusting a new ATS.

Uses the FAKE profile in docs/resume.example/ (Ada Example, a valid fake PDF) by
default, so running it never types your details into a live form. Nothing is
submitted -- Assist cannot submit. Nothing is written to jobs.db either: this
calls the assist path directly, not the web route that records `opened`.

NOT part of `pytest tests`: it needs Playwright, a browser and the network.
One page load per job id given, in a visible window, like clicking Assist.

    cd jobsearch
    python ../scripts/check_assist.py greenhouse:acme/123 ashby:org/uuid ...
    python ../scripts/check_assist.py --pick      # newest job per ATS

Env: SHOT_DIR (default: a temp dir), PROFILE_DIR (default: a temp copy of
docs/resume.example). Exit code is non-zero if a form yields no fields, or if
the resume's file name does not appear on the page after the upload, or an
uploader error does.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "jobsearch"))

from src import store as S                      # noqa: E402
from src.assist import browser as B             # noqa: E402
from src.assist import profile as P             # noqa: E402
from src.config import Config                   # noqa: E402

UPLOAD_ERR = re.compile(r"cannot read properties|upload failed|failed to upload|"
                        r"error uploading|file could not|try again", re.I)

PICKS = [
    "source='greenhouse' AND apply_url LIKE 'https://job-boards.greenhouse.io/%'",
    "source='greenhouse' AND apply_url NOT LIKE '%greenhouse.io%'",
    "source='ashby'",
    "source='lever'",
    "source='workable'",
]


def fake_profile(tmp: Path) -> Path:
    d = tmp / "resume"
    d.mkdir()
    for f in ("profile.yaml", "answers.yaml"):
        shutil.copy(ROOT / "docs" / "resume.example" / f, d / f)
    (d / "ada-example.pdf").write_bytes(fake_resume_pdf())
    P.pin(d, "ada-example.pdf")
    return d


def fake_resume_pdf() -> bytes:
    """A valid one-page PDF with a fake resume's text.

    It must be a REAL PDF: ATS uploaders parse the file, and the first live
    check used a page-less stub, which left it unknowable whether Greenhouse's
    upload error came from the file or from how it was attached. No dependency:
    the cross-reference offsets are computed here.
    """
    lines = ["Ada Example", "Software Engineer - Springfield, USA",
             "ada@example.com - +1 555 010 0199 - linkedin.com/in/ada-example", "",
             "EXPERIENCE", "Example Corp - Software Engineer - 2024 to present",
             "Built the thing that did the other thing.", "",
             "EDUCATION", "Example University - BSc Computer Science - 2019 to 2023",
             "", "SKILLS", "Python, TypeScript, SQL, PostgreSQL, Docker"]
    text = "BT /F1 11 Tf 72 740 Td 14 TL\n" + "".join(
        f"({ln}) Tj T*\n" for ln in lines) + "ET"
    objs = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
        b"/Resources<</Font<</F1 5 0 R>>>>/Contents 4 0 R>>",
        b"<</Length %d>>stream\n%s\nendstream" % (len(text), text.encode("latin-1")),
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (i, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objs) + 1, xref)
    return bytes(out)


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")   # form labels carry any character
    cfg = Config.load()
    conn = S.connect(cfg.db_path)
    ids = [a for a in argv if not a.startswith("--")]
    if "--pick" in argv:
        for q in PICKS:
            r = conn.execute(f"SELECT source_job_id FROM jobs WHERE {q} "
                             "AND (expired IS NULL OR expired = 0) "
                             "ORDER BY posted_ts DESC LIMIT 1").fetchone()
            if r:
                ids.append(r[0])
    if not ids:
        sys.exit(__doc__)

    tmp = Path(tempfile.mkdtemp(prefix="check_assist_"))
    pdir = Path(os.environ["PROFILE_DIR"]) if os.environ.get("PROFILE_DIR") \
        else fake_profile(tmp)
    shots = Path(os.environ.get("SHOT_DIR") or tmp)
    helper = B.Assistant(pdir, cfg.assist_hosts, browser_dir=tmp / "browser")
    bad = 0
    for job_id in ids:
        row = conn.execute("SELECT apply_url, title, company FROM jobs "
                           "WHERE source_job_id = ?", (job_id,)).fetchone()
        print(f"\n=== {job_id}  {row['title'] if row else '?'} @ "
              f"{row['company'] if row else '?'}")
        if row is None:
            print("  unknown job id"); bad += 1; continue
        try:
            target, resume = helper.prepare(job_id, row["apply_url"])
        except Exception as e:
            print(f"  refused: {e}"); continue
        print(f"  form: {target.url}")
        res = helper.assist(job_id, target, resume)
        print(f"  status: {res['status']}  {res.get('message', '')}")
        if res["status"] == "filled":
            print(f"  filled {res['filled']}, left {len(res['left'])} "
                  f"({res['required_left']} required), captcha widget: {res['widget']}")
            for f in res["left"]:
                req = "*" if f["required"] else " "
                hint = f"  -> {f['hint']}" if f.get("hint") else ""
                print(f"   {req} [{f['type']:<10}] {f['reason']:<15} "
                      f"{f['label'][:70]}{hint}")
            # Did the upload take? Uploaders work asynchronously, so give it a
            # moment, then look for the file name and for an uploader error.
            time.sleep(6)
            text = helper.page_text(job_id) or ""
            # case-insensitive: Lever renders the name as ADA-EXAMPLE.PDF
            shown = resume.path.name.lower() in text.lower()
            errs = [ln.strip() for ln in text.splitlines()
                    if UPLOAD_ERR.search(ln)][:3]
            print(f"  upload: {'file name shown on page' if shown else 'FILE NAME NOT SHOWN'}"
                  f"{'  ERRORS: ' + str(errs) if errs else ''}")
            if not shown or errs:
                bad += 1
        elif res["status"] == "no-form":
            bad += 1
        out = shots / (job_id.replace(":", "_").replace("/", "_") + ".png")
        if helper.screenshot(job_id, out):
            print(f"  screenshot: {out}")
    print(f"\nprofile dir: {pdir}\nunanswered.yaml:\n"
          + (pdir / P.INBOX).read_text(encoding="utf-8")
          if (pdir / P.INBOX).exists() else "\nno unanswered questions")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
