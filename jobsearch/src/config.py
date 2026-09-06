"""Configuration. Roles and locations are data, never constants.

Everything the crawler targets comes from targets.yaml, overridable per run on
the CLI. `ai-engineer` and `remote` are today's example, not the design -- there
is no role or location literal anywhere in the source.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
TARGETS_PATH = ROOT / "targets.yaml"
DB_PATH = ROOT / "jobs.db"
CACHE_DIR = ROOT / "cache"


def _env_path(var: str, default: Path) -> Path:
    """Let the corpus and cache live outside the checkout.

    Needed for an unattended deploy, where the code is a git checkout that gets
    replaced and the data is a volume that must not be. Unset means the
    repo-root default, which is what every local run and every test uses.
    """
    raw = os.environ.get(var)
    return Path(raw).expanduser() if raw else default


@dataclass
class Config:
    roles: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    max_pages_per_slice: int = 20
    delay_range: tuple[float, float] = (3.0, 5.0)
    user_agent: str = "jobsearch-personal/0.1"
    yield_floor: int = 1
    # Jobs older than this are not ingested, not enriched, and not shown.
    # 0 or null disables the cutoff entirely.
    max_age_days: int | None = 30
    # Per-source targets. Wellfound uses roles x locations above; the JSON-API
    # sources take their own shapes. See src/sources/.
    sources: dict = field(default_factory=dict)
    # role -> per-source query shape. See targets.yaml and ADR-009.
    role_map: dict = field(default_factory=dict)
    # How stale a cached LISTING may be before it is re-fetched, in hours.
    # Detail pages are never aged out, and a cached mitigation never expires
    # at all. 0 or null restores the old permanent cache. See ADR-011.
    cache_ttl_hours: float | None = 6.0

    # default_factory, not a bare default: the env is read per-Config, so a
    # test (or a systemd unit) can set it without reimporting the module.
    db_path: Path = field(default_factory=lambda: _env_path("JOBSEARCH_DB", DB_PATH))
    cache_dir: Path = field(
        default_factory=lambda: _env_path("JOBSEARCH_CACHE", CACHE_DIR))

    @classmethod
    def load(
        cls,
        path: Path | str = TARGETS_PATH,
        roles: str | None = None,
        locations: str | None = None,
        max_age_days: int | None = None,
    ) -> "Config":
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(
                f"{p} not found. It carries the roles and locations to crawl; "
                "copy targets.yaml from the repo root or pass --roles/--locations."
            )
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

        dr = raw.get("delay_range", [3.0, 5.0])
        cfg = cls(
            roles=list(raw.get("roles") or []),
            locations=list(raw.get("locations") or []),
            max_pages_per_slice=int(raw.get("max_pages_per_slice", 20)),
            delay_range=(float(dr[0]), float(dr[1])),
            user_agent=str(raw.get("user_agent") or "jobsearch-personal/0.1"),
            yield_floor=int(raw.get("yield_floor", 1)),
            # Absent key keeps the 30-day default; an explicit 0/null disables
            # the cutoff. Those are different intents and must not collapse.
            max_age_days=((int(raw["max_age_days"]) or None)
                          if "max_age_days" in raw else 30),
            sources=dict(raw.get("sources") or {}),
            role_map=dict(raw.get("role_map") or {}),
            # As with max_age_days, an absent key keeps the default while an
            # explicit 0/null disables the TTL. Different intents.
            cache_ttl_hours=((float(raw["cache_ttl_hours"]) or None)
                             if "cache_ttl_hours" in raw else 6.0),
        )
        # CLI overrides win over the file.
        if roles:
            cfg.roles = [r.strip() for r in roles.split(",") if r.strip()]
        if locations:
            cfg.locations = [l.strip() for l in locations.split(",") if l.strip()]

        if max_age_days is not None:
            cfg.max_age_days = max_age_days if max_age_days > 0 else None

        if cfg.delay_range[0] < 3.0:
            raise ValueError(
                f"delay_range floor is {cfg.delay_range[0]}s; Wellfound crawling is "
                "capped at 1 request per 3-5s. Refusing to go faster."
            )
        return cfg

    def cache_ttl_seconds(self) -> float | None:
        """Listing freshness window in seconds, or None to never expire."""
        return self.cache_ttl_hours * 3600.0 if self.cache_ttl_hours else None

    def role_query(self, role: str, source: str) -> str | None:
        """The source's own query token for a role, or None if it has none.

        None is meaningful and is not the same as "not configured": for RemoteOK
        it records that no tag returns jobs, verified rather than assumed.
        """
        return (self.role_map.get(role) or {}).get(source)

    def role_keywords(self, role: str) -> list[str]:
        """Keywords for the LOCAL relevance filter (RemoteOK, Himalayas).

        Empty means no filter, so a role missing from role_map keeps everything
        rather than silently returning nothing.
        """
        return list((self.role_map.get(role) or {}).get("keywords") or [])

    def role_categories(self, role: str) -> list[str]:
        return list((self.role_map.get(role) or {}).get("categories") or [])

    def source_enabled(self, name: str) -> bool:
        cfg = self.sources.get(name)
        if cfg is None:
            return name == "wellfound"   # the original, on by default
        return bool(cfg.get("enabled", True))

    def source_targets(self, name: str) -> list[dict]:
        """Targets to ingest for a source. Empty dict = 'everything it offers'."""
        cfg = self.sources.get(name) or {}
        targets = cfg.get("targets")
        if not targets:
            return [{}]
        return [t if isinstance(t, dict) else {"name": str(t)} for t in targets]

    def source_max_pages(self, name: str) -> int:
        cfg = self.sources.get(name) or {}
        return int(cfg.get("max_pages", self.max_pages_per_slice))

    def cutoff_ts(self) -> int | None:
        """Unix timestamp before which a job is considered stale, or None.

        Compared against the job's `liveStartAt`. Computed per call rather than
        cached so a long-running server does not drift.
        """
        if not self.max_age_days:
            return None
        import time

        return int(time.time()) - self.max_age_days * 86400

    def slices(self) -> list[tuple[str, str]]:
        """Every (role, location) pair to crawl. Redundant/overlapping slices are
        intentional: each gets its own ~15-page budget, which is how the per-slice
        cap is defeated."""
        return [(r, l) for r in self.roles for l in self.locations]
