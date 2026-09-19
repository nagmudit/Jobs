"""Assisted apply: pre-fill an ATS form in a visible browser; the user submits.

    profile.py   the user's profile, answers, resume pin and unanswered inbox
    match.py     form field descriptors -> a fill plan (pure, offline)
    browser.py   the Playwright side: extract, fill, never submit (optional dep)

See ADR-017 and docs/engineering/resume-and-answers.md.
"""
