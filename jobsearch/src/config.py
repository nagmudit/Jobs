"""Configuration. Roles and locations are data, never constants.

Everything the crawler targets comes from targets.yaml, overridable per run on
the CLI. `ai-engineer` and `remote` are today's example, not the design -- there
is no role or location literal anywhere in the source.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
TARGETS_PATH = ROOT / "targets.yaml"
DB_PATH = ROOT / "jobs.db"
CACHE_DIR = ROOT / "cache"


@dataclass
class Config:
    roles: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    max_pages_per_slice: int = 20
    delay_range: tuple[float, float] = (3.0, 5.0)
    user_agent: str = "jobsearch-personal/0.1"
    yield_floor: int = 1

    db_path: Path = DB_PATH
    cache_dir: Path = CACHE_DIR

    @classmethod
    def load(
        cls,
        path: Path | str = TARGETS_PATH,
        roles: str | None = None,
        locations: str | None = None,
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
        )
        # CLI overrides win over the file.
        if roles:
            cfg.roles = [r.strip() for r in roles.split(",") if r.strip()]
        if locations:
            cfg.locations = [l.strip() for l in locations.split(",") if l.strip()]

        if cfg.delay_range[0] < 3.0:
            raise ValueError(
                f"delay_range floor is {cfg.delay_range[0]}s; Wellfound crawling is "
                "capped at 1 request per 3-5s. Refusing to go faster."
            )
        return cfg

    def slices(self) -> list[tuple[str, str]]:
        """Every (role, location) pair to crawl. Redundant/overlapping slices are
        intentional: each gets its own ~15-page budget, which is how the per-slice
        cap is defeated."""
        return [(r, l) for r in self.roles for l in self.locations]
