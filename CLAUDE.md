@AGENTS.md

# Claude Code

Follow `AGENTS.md`. Additionally:

- Use plan mode for changes crossing `fetch` / `crawl` / `store` / the `jobs` view.
- Prefer Grep/Glob over shell `find`/`grep`.
- Long crawls belong in a background Bash task — they run for tens of minutes at the
  mandated 3–5 s spacing and must not be sped up to fit a timeout.
- Update the active execution plan before reporting completion.
