import logging
from datetime import datetime
from typing import Optional, Tuple

from adapters.base import BaseAdapter

logger = logging.getLogger(__name__)

FDA_510K_URL  = "https://api.fda.gov/device/510k.json"
DEFAULT_LIMIT = 100


class FDA510KAdapter(BaseAdapter):
    """
    Fetches 510(k) device clearances from the openFDA API.
    Stores raw JSON response to Bronze — zero processing.

    # ── TODO: Watermarking ────────────────────────────────────────────────
    # openFDA supports server-side date filtering via Lucene range syntax:
    #   search=decision_date:[{since} TO *]
    #
    # Implementation steps when ready:
    #   1. Load watermark:  last = WatermarkStore.get(self.source_id)
    #   2. Pass to run():   run(source_id, since=last)
    #   3. The `since` → search filter wiring is already done below.
    #   4. After successful run, persist the newest decision_date seen:
    #        WatermarkStore.set(self.source_id, newest_date_seen)
    #
    # Unlike ClinicalTrials, the filter is server-side — no client-side
    # fallback needed.  WatermarkStore: flat JSON locally; DynamoDB on AWS.
    # ─────────────────────────────────────────────────────────────────────
    """

    source_id:       str = "fda_510k"
    source_category: str = "regulatory"
    version:         str = "1.0.0"
    file_extension:  str = "json"

    def __init__(self, limit: int = DEFAULT_LIMIT):
        super().__init__()
        self.limit = limit

    def fetch(self, since: Optional[datetime] = None) -> Tuple[str, str]:
        """
        Fetch recent 510(k) clearances and return (raw_json, extension).

        Args:
            since: if provided, filters to records where decision_date >= this
                   date using openFDA's Lucene range syntax.
        """
        params = {
            "limit": self.limit,
            "sort":  "decision_date:desc",
        }

        if since is not None:
            params["search"] = f"decision_date:[{since.strftime('%Y-%m-%d')} TO *]"
            logger.info(
                "[%s] Fetching up to %d clearances with decision_date >= %s",
                self.source_id, self.limit, since.strftime("%Y-%m-%d"),
            )
        else:
            logger.info(
                "[%s] Fetching up to %d clearances (no date filter)",
                self.source_id, self.limit,
            )

        resp = self._get(FDA_510K_URL, params=params)

        if resp.status_code != 200:
            raise RuntimeError(
                f"[{self.source_id}] openFDA returned HTTP {resp.status_code} — "
                f"not writing to Bronze"
            )

        raw  = resp.text

        logger.info(
            "[%s] Fetched %d bytes",
            self.source_id, len(raw.encode("utf-8")),
        )
        return raw, self.file_extension