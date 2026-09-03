# Drift Detection and Writeback

Documentation decays silently. These checks find the decay; the writeback protocol slows it.

## Checks

Ordered by damage caused when they fail. Run the mechanical ones first — they're cheap and catch most of it.

### Mechanical

| Check | How | Severity |
|---|---|---|
| Dead paths | Every `path/like/this` in docs → does it exist? | P1 |
| Broken links | Relative markdown links resolve | P2 |
| Unverified commands | Every documented command exists in package scripts / Makefile / CI | P0 |
| Duplicate ADR IDs | Two `ADR-004` files | P2 |
| Orphan docs | Files under `docs/` absent from `index.md` | P2 |
| Stale `last_verified` | Older than ~90 days on `status: current` | P2 |

`grep -oE '\b(src|app|lib|services|packages)/[A-Za-z0-9_/.-]+' docs/ -r` then test each path. Highest hit rate of any check — paths break on every refactor.

### Secrets

Scan docs for anything resembling a live credential: `sk-`, `AKIA`, `ghp_`, `xox[baprs]-`, private key headers, connection strings with inline passwords, JWTs. **P0, always.** Documentation gets pasted into issues and shared publicly far more casually than code.

### Semantic

Costlier, needs judgment.

| Check | How | Severity |
|---|---|---|
| Phantom features | Docs describe behavior that doesn't exist in code | P0 |
| Missing surfaces | Routes/models/integrations in code, absent from docs | P1 |
| Env drift | Documented env vars vs. actual reads in code, both directions | P1 |
| Contradictions | `CLAUDE.md` or Copilot instructions disagreeing with `AGENTS.md` | P0 |
| Stale plans | `active/` plan with completion criteria met, or untouched 60+ days | P1 |
| Unenforced rules | A documented "must" with no test or lint behind it | P2 |
| Bloat | `AGENTS.md` past ~200 lines | P2 |

Sample rather than exhaust: spot-check the highest-traffic claims. Note in Limitations what you didn't check.

### The verdict question

Beneath the table, answer one thing: **could an agent land a correct change here knowing only the repo?** If not, name the single document blocking it. That answer is what people act on.

## Contradiction handling

When docs and code disagree:

1. Record the conflict before touching anything.
2. Fix only unambiguous drift — a renamed path, a removed script, a dead link.
3. **Escalate genuine disagreement.** If the code might be the bug, say so. Never rewrite docs to match suspected-broken behavior, and never change code to satisfy stale docs.
4. Don't rewrite history. A superseded decision gets marked superseded with a pointer, not deleted.

The instinct to make everything consistent is exactly wrong here. An honestly recorded contradiction is more useful than a false resolution, because it directs attention at the real ambiguity.

## Writeback protocol

Documentation is part of implementing a change, not cleanup afterward. Put this in `AGENTS.md` so every future agent inherits it.

Before declaring work complete, check whether the change touched:

product behavior · requirements · business rules · architecture · API contracts · database schema · integrations · security or permissions · env vars · testing · deployment · observability · runbooks · technical debt · active plans

For each hit, update the authoritative document in the same task. Update the primary source and link from elsewhere — duplicating content across documents guarantees they diverge.

If nothing needed updating, say why. That sentence is what stops "no docs changed" from being the default outcome.

**Optional enforcement (Tier 2+):** a `Stop` hook that blocks completion while `docs/plans/active/` holds a plan whose `last_updated` predates the session's commits. Offer it; don't install it without asking — hooks change how the harness behaves for every future session in that repo.

## Gardening

Periodically, or when a repo feels untrustworthy:

- Archive documents describing removed features.
- Merge duplicate sources of truth into one, leaving pointers.
- Promote repeatedly-violated prose rules into lint rules or tests.
- Demote docs nobody has verified in a year from `current` to `historical`.
- Split oversized `AGENTS.md` into root plus nested files.
- Delete generated artifacts nobody reads.

Deletion is a legitimate outcome. A smaller accurate system beats a larger stale one, and this skill's most common failure is generating more than the repo can keep true.

For Tier 3, put the mechanical checks in `scripts/context/validate-context` and run it in CI. Prose asks; automation enforces.
