#!/usr/bin/env bash
# Vercel build step: fetch the corpus that GitHub Actions published.
#
# The database is NOT in git -- it is ~32 MB and rebuilt daily, so committing it
# would add a gigabyte of history a month. Actions uploads it to the `corpus`
# release tag and pings a Vercel deploy hook; this pulls it into the bundle.
#
# Set CORPUS_URL in the Vercel project's environment variables, e.g.
#   https://github.com/<owner>/<repo>/releases/download/corpus/corpus.db
#
# Failing here is correct: a deploy that serves an empty corpus is worse than no
# deploy, and Vercel keeps the previous deployment live when a build fails.
set -euo pipefail

if [ -z "${CORPUS_URL:-}" ]; then
  echo "CORPUS_URL is not set -- cannot fetch the corpus." >&2
  exit 1
fi

echo "Downloading corpus from ${CORPUS_URL}"
curl -fsSL --retry 3 -o jobsearch/jobs.db "$CORPUS_URL"

# A truncated download would deploy a corrupt database that only fails at query
# time, on the user's first page load.
size=$(wc -c < jobsearch/jobs.db)
echo "Corpus is ${size} bytes"
[ "$size" -gt 1000000 ] || { echo "corpus looks truncated (${size} bytes)" >&2; exit 1; }
python3 -c "import sqlite3,sys; sqlite3.connect('jobsearch/jobs.db').execute('PRAGMA quick_check').fetchone()"
echo "Corpus OK"
