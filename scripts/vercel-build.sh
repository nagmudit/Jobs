#!/usr/bin/env bash
# Vercel build step: fetch the corpus that GitHub Actions published.
#
# The database is NOT in git -- it is tens of MB and rebuilt daily, so committing
# it would add a gigabyte of history a month. Actions uploads it to the `corpus`
# release tag and pings a Vercel deploy hook; this pulls it into the bundle.
#
# The URL is derived from the repository Vercel is building, so a public repo
# needs no configuration at all. Set CORPUS_URL to override that -- a private
# repo, a different tag, or a corpus hosted somewhere else entirely.
#
# Failing here is correct: a deploy that serves an empty or truncated corpus is
# worse than no deploy, and Vercel keeps the previous deployment live when a
# build fails.
set -euo pipefail

TAG="${CORPUS_TAG:-corpus}"
ASSET="${CORPUS_ASSET:-corpus.db}"

if [ -z "${CORPUS_URL:-}" ]; then
  owner="${VERCEL_GIT_REPO_OWNER:-}"
  slug="${VERCEL_GIT_REPO_SLUG:-}"
  if [ -n "$owner" ] && [ -n "$slug" ]; then
    CORPUS_URL="https://github.com/${owner}/${slug}/releases/download/${TAG}/${ASSET}"
    echo "Deriving corpus URL from the connected repository."
  fi
fi

if [ -z "${CORPUS_URL:-}" ]; then
  cat >&2 <<'MSG'
Cannot work out where the corpus lives.

This build is not git-connected (so VERCEL_GIT_REPO_OWNER/SLUG are unset) and
CORPUS_URL is not set. Set CORPUS_URL in the Vercel project's environment
variables, for example:

  https://github.com/<owner>/<repo>/releases/download/corpus/corpus.db
MSG
  exit 1
fi

echo "Downloading corpus from ${CORPUS_URL}"
if ! curl -fsSL --retry 3 -o jobsearch/jobs.db "$CORPUS_URL"; then
  cat >&2 <<MSG

Could not download ${CORPUS_URL}.

If this is a 404 the release does not exist yet. The corpus is published by the
daily workflow; seed it once by hand from a local checkout:

  cd jobsearch && python -m src.cli export --out ../corpus.db && cd ..
  gh release create ${TAG} --title Corpus --notes "Rebuilt automatically."
  gh release upload ${TAG} corpus.db --clobber

A private repository needs an authenticated URL instead.
MSG
  exit 1
fi

# A truncated download would deploy a corrupt database that only fails at query
# time, on the reader's first page load.
size=$(wc -c < jobsearch/jobs.db)
echo "Corpus is ${size} bytes"
[ "$size" -gt 1000000 ] || { echo "corpus looks truncated (${size} bytes)" >&2; exit 1; }
python3 -c "import sqlite3,sys; sqlite3.connect('jobsearch/jobs.db').execute('PRAGMA quick_check').fetchone()"
echo "Corpus OK"
