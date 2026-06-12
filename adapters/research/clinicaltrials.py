import logging
from datetime import datetime
from typing import Optional, Tuple

from adapters.base import BaseAdapter

logger = logging.getLogger(__name__)

CT_API_BASE   = "https://clinicaltrials.gov/api/v2/studies"
DEFAULT_LIMIT = 100   # studies per run; raise once watermarking is live


class ClinicalTrialsAdapter(BaseAdapter):
    """
    Fetches clinical trials from ClinicalTrials.gov v2 REST API.
    Stores the raw JSON response to Bronze — zero processing.

    # ── TODO: Watermarking ────────────────────────────────────────────────
    # The v2 API has no server-side date filter parameter.
    # Filtering is done client-side after fetch.
    #
    # Implementation steps when ready:
    #   1. Load watermark:  last = WatermarkStore.get(self.source_id)
    #   2. Pass to run():   run(source_id, since=last)
    #   3. In the normaliser map, after parsing each study check:
    #        if study["lastUpdatePostDate"] < last: stop processing
    #      (studies arrive newest-first due to sort=LastUpdatePostDate:desc)
    #   4. After a successful run, persist the newest date seen:
    #        WatermarkStore.set(self.source_id, newest_date_seen)
    #
    # WatermarkStore: flat JSON file locally; DynamoDB item on AWS.
    # ─────────────────────────────────────────────────────────────────────
    """

    source_id:       str = "clinicaltrials"
    source_category: str = "research"
    version:         str = "1.0.0"
    file_extension:  str = "json"

    def __init__(self, page_size: int = DEFAULT_LIMIT):
        super().__init__()
        self.page_size = page_size

    def fetch(self, since: Optional[datetime] = None) -> Tuple[str, str]:
        """
        Fetch the most recently updated studies and return (raw_json, extension).

        Args:
            since: reserved for watermarking — not used as an API filter
                   (the v2 API has no server-side date filter).
                   Client-side filtering against this date will be added
                   to the normaliser map when the WatermarkStore is built.
        """
        params = {
            "format":   "json",
            "pageSize": self.page_size,
            "sort":     "LastUpdatePostDate:desc",
        }

        logger.info(
            "[%s] Fetching up to %d studies (sort: LastUpdatePostDate desc)",
            self.source_id, self.page_size,
        )

        resp = self._get(CT_API_BASE, params=params)
        raw  = resp.text

        logger.info(
            "[%s] Fetched %d bytes",
            self.source_id, len(raw.encode("utf-8")),
        )
        return raw, self.file_extension