import json
import logging
from datetime import datetime
from typing import Optional, Tuple

from adapters.base import BaseAdapter

logger = logging.getLogger(__name__)

COMTRADE_PREVIEW_URL = "https://comtradeapi.un.org/public/v1/preview/C/A/HS"

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
DEFAULT_PERIOD = "2024"    # Most recent year with reliable annual data


class ComtradeAdapter(BaseAdapter):
    """
    Fetches medical device and pharma trade data from the UN Comtrade
    public preview API.  No subscription key required.

    Makes 12 calls per run (6 HS codes × 2 flows) and combines all
    responses into a single Bronze JSON envelope.

    Free API limit: ~500 records per query.  Full bilateral trade data
    requires a paid subscription; preview data covers world-level and
    major reporter/partner combinations.

    # ── TODO: Watermarking ────────────────────────────────────────────────
    # UN Comtrade data is annual — watermarking increments the year.
    # When ready:
    #   1. Load last fetched year: WatermarkStore.get(self.source_id)
    #   2. Pass next year to run(): run(source_id, since=datetime(year,1,1))
    #   3. After run, store: WatermarkStore.set(self.source_id, year)
    # ─────────────────────────────────────────────────────────────────────
    """

    source_id:        str   = "comtrade"
    source_category:  str   = "trade"
    version:          str   = "1.0.0"
    file_extension:   str   = "json"
    rate_limit_delay: float = 2.0   # be polite to public API

    def __init__(self, period: str = DEFAULT_PERIOD):
        super().__init__()
        self.period = period

    def fetch(self, since: Optional[datetime] = None) -> Tuple[str, str]:
        """
        Fetch all 12 (HS code × flow) combinations.

        Args:
            since: if provided, uses since.year - 1 as the trade period
                   to ensure full annual data is available.
        """
        if since is not None:
            # Use the year before `since` to ensure complete annual data.
            # Annual data for year Y is typically available mid-year Y+1.
            period = str(max(since.year - 1, 2020))
        else:
            period = self.period

        logger.info(
            "[%s] Fetching %d HS codes × %d flows for period %s",
            self.source_id, len(CMD_CODES), len(FLOW_CODES), period,
        )

        queries = []
        for cmd_code, cmd_desc in CMD_CODES.items():
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
            "fetched_at": datetime.utcnow().isoformat(),
            "queries":    queries,
        }, ensure_ascii=False)

        return combined, self.file_extension

    def _fetch_one(self, cmd_code: str, flow_code: str, period: str) -> dict:
        """Fetch one (HS code, flow) combination. Returns dict with records."""
        params = {
            "cmdCode":  cmd_code,
            "flowCode": flow_code,
            "period":   period,
        }
        try:
            resp = self._get(COMTRADE_PREVIEW_URL, params=params)
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