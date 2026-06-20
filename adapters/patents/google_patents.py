import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from google.cloud import bigquery

from adapters.base import BaseAdapter

logger = logging.getLogger(__name__)

# Public dataset — no API key required, but BigQuery billing/quota still
# applies to the querying GCP project (auth via GOOGLE_APPLICATION_CREDENTIALS,
# a service-account key with the "BigQuery Job User" role — see debug scripts).
SOURCE_TABLE = "patents-public-data.patents.publications"
GOOGLE_CREDENTIALS_ENV = "GOOGLE_APPLICATION_CREDENTIALS"

# Pinned explicitly rather than left to ADC's default-project resolution —
# a stray GOOGLE_CLOUD_PROJECT/GCLOUD_PROJECT env var (e.g. left over from
# an unrelated Gemini/AI-Studio API key setup) would otherwise silently
# redirect billing to the wrong project while auth still succeeds.
BQ_BILLING_PROJECT = "whayne-big-query"

# ── Scope decision (see debug_google_patents_bq_*.py investigation) ───────
# Table is NOT time-partitioned/clustered — date and country filters do not
# reduce bytes scanned (cost is driven by which COLUMNS are read across all
# 170M+ rows, not which rows survive the WHERE clause). Selecting
# title_localized + abstract_localized costs ~258GB regardless of filters.
# That is what we pay for, once, per run. Filters below control row COUNT
# (what we keep and store), not query cost.
#
# CPC A61* alone = ~12.2M matching publications. Restricting to
# US/EP/WO + a 24-month window brings that to ~404K rows — workable at
# Aurora scale, comparable to Comtrade's ~413K. India (IN) returns ~3 rows;
# Google Patents Public Data has essentially no Indian Patent Office
# coverage — this is a real source limitation, not a query bug.
CPC_PATTERN = "A61%"
COUNTRIES = ["US", "EP", "WO"]
LOOKBACK_DAYS = 730  # ~24 months, matching the plan's backfill assumption

# Hard safety cap on bytes billed. Observed actual cost for this query
# shape is ~258GB; capped well above that but still a real stop against
# an unexpectedly larger query (e.g. a future schema/field change).
MAX_GB_SAFETY = 300

QUERY = f"""
SELECT
  publication_number,
  application_number,
  country_code,
  kind_code,
  application_kind,
  family_id,
  pct_number,
  publication_date,
  filing_date,
  grant_date,
  priority_date,
  title_localized,
  abstract_localized,
  assignee_harmonized,
  inventor_harmonized,
  cpc
FROM `{SOURCE_TABLE}`
WHERE country_code IN UNNEST(@countries)
  AND publication_date >= @cutoff_date
  AND EXISTS (
    SELECT 1 FROM UNNEST(cpc) AS c WHERE c.code LIKE @cpc_pattern
  )
"""


class GooglePatentsAdapter(BaseAdapter):
    """
    Fetches medical-device/pharma-relevant patent publications from
    Google Patents Public Data (BigQuery), scoped to CPC A61* in
    US/EP/WO over a rolling window.

    Not an HTTP source — uses the BigQuery client SDK directly rather
    than BaseAdapter._get(). Requires GOOGLE_APPLICATION_CREDENTIALS
    pointing at a service-account key with "BigQuery Job User".

    Cost discipline: every run dry-runs the query first (free) and
    aborts rather than bills if the estimate exceeds MAX_GB_SAFETY.
    Expected actual cost ~258GB/run — about a quarter of the 1TB/month
    free tier, even run monthly.

    Excludes claims_localized/description_localized — large columns,
    not needed for canonical title/summary; full text is retrievable
    later by publication_number if ever required.

    # ── TODO: Watermarking ────────────────────────────────────────────
    # Currently re-fetches the full rolling LOOKBACK_DAYS window every
    # run (idempotent — content_hash dedup handles re-ingestion safely,
    # but it re-pays the ~258GB scan cost each time).
    #   1. Load last watermark: WatermarkStore.get(self.source_id)
    #   2. Pass as `since` to fetch() to narrow @cutoff_date
    #   3. After run, store the latest publication_date seen
    # ─────────────────────────────────────────────────────────────────
    """

    source_id:        str   = "google_patents"
    source_category:  str   = "patents"
    version:          str   = "1.0.0"
    file_extension:   str   = "json"
    rate_limit_delay: float = 0.0  # unused — single batched query, not per-record HTTP calls

    def __init__(self, countries: Optional[List[str]] = None):
        super().__init__()
        if not os.environ.get(GOOGLE_CREDENTIALS_ENV):
            logger.warning(
                "[%s] %s not set — BigQuery client will fail to authenticate",
                self.source_id, GOOGLE_CREDENTIALS_ENV,
            )
        self.client = bigquery.Client(project=BQ_BILLING_PROJECT)
        # Override for splitting a large run by country (see MemoryError
        # note below) or for testing on a smaller slice.
        self.countries = countries or COUNTRIES

    def fetch(self, since: Optional[datetime] = None) -> Tuple[str, str]:
        cutoff = since or (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=LOOKBACK_DAYS))
        cutoff_int = int(cutoff.strftime("%Y%m%d"))

        query_params = [
            bigquery.ArrayQueryParameter("countries", "STRING", self.countries),
            bigquery.ScalarQueryParameter("cutoff_date", "INT64", cutoff_int),
            bigquery.ScalarQueryParameter("cpc_pattern", "STRING", CPC_PATTERN),
        ]

        logger.info(
            "[%s] Querying BigQuery: countries=%s cpc=%s publication_date>=%d",
            self.source_id, self.countries, CPC_PATTERN, cutoff_int,
        )

        dry_run_config = bigquery.QueryJobConfig(
            query_parameters=query_params, dry_run=True, use_query_cache=False,
        )
        dry_run_job = self.client.query(QUERY, job_config=dry_run_config)
        gb_estimate = dry_run_job.total_bytes_processed / 1e9
        logger.info("[%s] Dry run estimate: %.1f GB", self.source_id, gb_estimate)

        if gb_estimate > MAX_GB_SAFETY:
            raise RuntimeError(
                f"[{self.source_id}] Query estimate {gb_estimate:.1f}GB exceeds "
                f"safety cap {MAX_GB_SAFETY}GB — aborting before billing. "
                f"Check whether the table schema or query shape changed."
            )

        run_config = bigquery.QueryJobConfig(
            query_parameters=query_params,
            maximum_bytes_billed=int(MAX_GB_SAFETY * 1e9),
        )
        job = self.client.query(QUERY, job_config=run_config)
        rows = job.result()

        # ── Stream-build the Bronze JSON ────────────────────────────────
        # Previously: build a full list of nested dicts, THEN json.dumps()
        # the whole thing — holding the expanded Python object graph AND
        # the full serialised string in memory simultaneously. At ~400K
        # richly-nested records (multi-language title/abstract, CPC and
        # actor lists per record) that was enough to raise a MemoryError.
        # Serialising row-by-row means each row's dict is discarded right
        # after being written into the buffer, instead of accumulating —
        # and iterating `rows` here is also where the slow network
        # pagination happens, so this doubles as progress visibility.
        scope_json = json.dumps({
            "countries":             self.countries,
            "cpc_pattern":            CPC_PATTERN,
            "publication_date_from": cutoff_int,
        })
        fetched_at_json = json.dumps(datetime.now(timezone.utc).replace(tzinfo=None).isoformat())

        parts: List[str] = [
            '{"scope": ', scope_json,
            ', "fetched_at": ', fetched_at_json,
            ', "records": [',
        ]
        count = 0
        for row in rows:
            if count:
                parts.append(",")
            parts.append(json.dumps(self._row_to_dict(row), ensure_ascii=False))
            count += 1
            if count % 50_000 == 0:
                logger.info("[%s] ...%d records fetched/serialised so far", self.source_id, count)
        parts.append(f'], "record_count": {count}}}')

        logger.info("[%s] Fetched %d records", self.source_id, count)

        return "".join(parts), self.file_extension

    @staticmethod
    def _row_to_dict(row) -> Dict[str, Any]:
        """Convert a BigQuery Row to a plain JSON-serialisable dict.
        Nested RECORD/REPEATED fields come back dict-like already;
        dict(x) normalises them defensively for json.dumps()."""
        return {
            "publication_number":  row.publication_number,
            "application_number":  row.application_number,
            "country_code":        row.country_code,
            "kind_code":            row.kind_code,
            "application_kind":     row.application_kind,
            "family_id":            row.family_id,
            "pct_number":           row.pct_number,
            "publication_date":     row.publication_date,
            "filing_date":          row.filing_date,
            "grant_date":           row.grant_date,
            "priority_date":        row.priority_date,
            "title_localized":      [dict(t) for t in (row.title_localized or [])],
            "abstract_localized":   [dict(a) for a in (row.abstract_localized or [])],
            "assignee_harmonized":  [dict(a) for a in (row.assignee_harmonized or [])],
            "inventor_harmonized":  [dict(i) for i in (row.inventor_harmonized or [])],
            "cpc":                  [dict(c) for c in (row.cpc or [])],
        }