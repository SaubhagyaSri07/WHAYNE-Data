import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional, Tuple

from adapters.base import BaseAdapter

logger = logging.getLogger(__name__)

# Basic Individual (free, registered) tier:
#   - Data API access (not just preview)
#   - up to 100,000 records per call
#   - 500 calls/day, 1 call/second
#   - includeDesc=true returns real reporter/partner/commodity names
COMTRADE_DATA_URL = "https://comtradeapi.un.org/data/v1/get/C/A/HS"

# API key is read from environment — never hardcode in source.
# Set it before running the pipeline:
#   PowerShell:  $env:COMTRADE_API_KEY = "your_key_here"
#   Persistent:  [Environment]::SetEnvironmentVariable("COMTRADE_API_KEY","your_key","User")
COMTRADE_API_KEY_ENV = "COMTRADE_API_KEY"

# HS commodity codes relevant to medical devices and pharma
CMD_CODES = {
    "9018": "Medical/surgical instruments and appliances",
    "9019": "Mechano-therapy appliances; massage apparatus",
    "9020": "Breathing appliances and gas masks",
    "3004": "Pharmaceutical preparations (retail packs)",
    "3002": "Vaccines, blood products, antisera",
    "3822": "Diagnostic or laboratory reagents",
}

FLOW_CODES = ["X", "M"]   # Export, Import
DEFAULT_PERIOD = "2024"


class ComtradeAdapter(BaseAdapter):
    """
    Fetches medical device and pharma trade data from the UN Comtrade
    Data API (Basic Individual / free registered tier).

    Requires COMTRADE_API_KEY environment variable — register for free
    at https://comtradeplus.un.org/ → Developer Portal → "Free APIs" product.

    Upgrades over the anonymous preview tier:
      - includeDesc=true returns real reporterDesc/partnerDesc/cmdDesc/flowDesc
        (no M49 lookup table needed)
      - up to 100,000 records per call (was capped at 500)
      - 500 calls/day, 1 call/sec rate limit

    Makes 12 calls per run (6 HS codes × 2 flows), combined into a
    single Bronze JSON envelope.

    # ── TODO: Watermarking ────────────────────────────────────────────────
    # UN Comtrade data is annual. The getDa endpoint additionally supports
    # publishedDateFrom/publishedDateTo for tracking newly-revised data.
    # For now: increment the period (year) each run.
    #   1. Load last fetched year: WatermarkStore.get(self.source_id)
    #   2. Pass next year:  run(source_id, since=datetime(year, 1, 1))
    #   3. After run, store: WatermarkStore.set(self.source_id, year)
    # ─────────────────────────────────────────────────────────────────────
    """

    source_id:        str   = "comtrade"
    source_category:  str   = "trade"
    version:          str   = "2.0.0"
    file_extension:   str   = "json"
    rate_limit_delay: float = 1.5   # within 1 call/sec limit

    def __init__(self, period: str = DEFAULT_PERIOD):
        super().__init__()
        self.period = period
        self.api_key = os.environ.get(COMTRADE_API_KEY_ENV, "")
        if not self.api_key:
            logger.warning(
                "[%s] %s environment variable not set — requests will fail",
                self.source_id, COMTRADE_API_KEY_ENV,
            )
        else:
            self.session.headers.update({
                "Ocp-Apim-Subscription-Key": self.api_key,
            })

    def fetch(self, since: Optional[datetime] = None) -> Tuple[str, str]:
        """
        Fetch all 12 (HS code × flow) combinations with full descriptions.

        Args:
            since: if provided, uses since.year - 1 as the trade period
                   to ensure full annual data is available.
        """
        if since is not None:
            period = str(max(since.year - 1, 2020))
        else:
            period = self.period

        logger.info(
            "[%s] Fetching %d HS codes × %d flows for period %s (Data API, includeDesc=true)",
            self.source_id, len(CMD_CODES), len(FLOW_CODES), period,
        )

        queries = []
        for cmd_code in CMD_CODES:
            for flow_code in FLOW_CODES:
                result = self._fetch_one(cmd_code, flow_code, period)
                queries.append(result)

        total_records = sum(len(q.get("records", [])) for q in queries)
        logger.info(
            "[%s] Fetched %d total records across %d queries",
            self.source_id, total_records, len(queries),
        )

        combined = json.dumps({
            "period":     period,
            "fetched_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
            "queries":    queries,
        }, ensure_ascii=False)

        return combined, self.file_extension

    def _fetch_one(self, cmd_code: str, flow_code: str, period: str) -> dict:
        """Fetch one (HS code, flow) combination. Returns dict with records."""
        params = {
            "cmdCode":     cmd_code,
            "flowCode":    flow_code,
            "period":      period,
            "includeDesc": "true",
        }
        try:
            resp = self._get(COMTRADE_DATA_URL, params=params)
            data = resp.json()
            records = data.get("data", [])
            logger.info(
                "[%s] HS%s %s → %d records",
                self.source_id, cmd_code, flow_code, len(records),
            )
            return {
                "cmd_code":  cmd_code,
                "cmd_desc":  CMD_CODES[cmd_code],
                "flow_code": flow_code,
                "period":    period,
                "status":    resp.status_code,
                "records":   records,
            }
        except Exception as e:
            logger.warning(
                "[%s] HS%s %s failed: %s",
                self.source_id, cmd_code, flow_code, e,
            )
            return {
                "cmd_code":  cmd_code,
                "cmd_desc":  CMD_CODES[cmd_code],
                "flow_code": flow_code,
                "period":    period,
                "status":    None,
                "records":   [],
            }