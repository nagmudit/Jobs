"""Wellfound source: the original. Search pages, Apollo cache, three URL shapes.

The ingest path lives in `src/crawl.py` (it carries the four corpus-integrity
assertions and the slice telemetry, none of which generalise to a JSON API), so
this module supplies the core view only.
"""

from __future__ import annotations

NAME = "wellfound"

# Maps the JobListingSearchResult / StartupResult shape onto CORE_COLUMNS.
# Column ORDER is the contract -- see src/sources/__init__.py.
CORE_VIEW_SQL = """
SELECT
  'wellfound'                                              AS source,
  j.source_job_id                                          AS source_job_id,
  j.native_id                                              AS native_id,
  j.company_slug                                           AS company_slug,
  json_extract(j.raw_json,'$.title')                       AS title,
  json_extract(c.raw_json,'$.name')                        AS company,
  size_label(json_extract(c.raw_json,'$.companySize'))     AS company_size,
  size_min(json_extract(c.raw_json,'$.companySize'))       AS company_size_min,
  NULLIF(json_extract(c.raw_json,'$.highConcept'),'')      AS high_concept,
  (SELECT group_concat(value,', ') FROM json_each(c.raw_json,'$._badges')) AS badges,
  (SELECT group_concat(value,', ') FROM json_each(j.raw_json,'$.locationNames')) AS location_raw,
  (SELECT group_concat(value,', ') FROM json_each(j.raw_json,'$.acceptedRemoteLocationNames')) AS remote_locations,
  json_extract(j.raw_json,'$.remote')                      AS remote,
  json_extract(j.raw_json,'$.remoteConfig.kind')           AS remote_config,
  remote_label(json_extract(j.raw_json,'$.remoteConfig.kind'),
               json_extract(j.raw_json,'$.remote'))        AS remote_label,
  NULLIF(json_extract(j.raw_json,'$.jobType'),'')          AS job_type,
  NULLIF(json_extract(j.raw_json,'$.compensation'),'')     AS salary_raw,
  NULL                                                     AS salary_min_native,
  NULL                                                     AS salary_max_native,
  -- Wellfound embeds the currency symbol in the compensation string; derive.py
  -- parses the magnitude but the currency is not extracted. Left NULL rather
  -- than guessed. See GAP: cross-currency salary sorting.
  NULL                                                     AS salary_currency,
  NULL                                                     AS salary_period,
  json_extract(j.raw_json,'$.liveStartAt')                 AS posted_ts,
  NULL                                                     AS expires_ts,
  json_extract(j.raw_json,'$.description')                 AS description,
  json_extract(j.raw_json,'$.atsSource')                   AS ats_source,
  json_extract(j.raw_json,'$.primaryRoleTitle')            AS primary_role_title,
  json_extract(j.raw_json,'$.autoPosted')                  AS auto_posted,
  'https://wellfound.com/jobs/' || j.native_id || '-' ||
      COALESCE(json_extract(j.raw_json,'$.slug'),'')       AS apply_url,
  j.first_seen                                             AS first_seen,
  j.last_seen                                              AS last_seen
FROM job_raw j
LEFT JOIN company_raw c
  ON c.company_slug = j.company_slug AND c.source = 'wellfound'
WHERE j.source = 'wellfound'
"""
