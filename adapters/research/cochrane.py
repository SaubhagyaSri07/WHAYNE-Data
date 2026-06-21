import json
import logging
from datetime import datetime
from typing import Optional, Tuple

from adapters.base import BaseAdapter

logger = logging.getLogger(__name__)

EPMC_SEARCH_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

# ISSN 1469-493X = Cochrane Database of Systematic Reviews (online).
# JOURNAL_TITLE / JOURNAL / JRNL are not valid Europe PMC CQL fields.
# sort parameter omitted: "FIRST_PDATE desc" URL-encodes the space as
# "+" which Europe PMC rejects silently, returning {"version":"6.9"}.
COCHRANE_QUERY    = "ISSN:1469-493X"
DEFAULT_PAGE_SIZE = 100


class CochraneAdapter(BaseAdapter):
    """
    Fetches Cochrane systematic reviews from the Europe PMC REST API.
    Stores raw JSON response to Bronze — zero processing.

    # ── TODO: Watermarking ────────────────────────────────────────────────
    # Europe PMC supports server-side date filtering via CQL:
    #   AND (FIRST_PDATE:[{since} TO *])
    # The `since` → date filter wiring is already done below.
    # Implementation steps when ready:
    #   1. Load watermark:  last = WatermarkStore.get(self.source_id)
    #   2. Pass to run():   run(source_id, since=last)
    #   3. After successful run, persist newest FIRST_PDATE seen:
    #        WatermarkStore.set(self.source_id, newest_date_seen)
    # ─────────────────────────────────────────────────────────────────────
    """

    source_id:       str = "cochrane"
    source_category: str = "research"
    version:         str = "1.0.0"
    file_extension:  str = "json"

    def __init__(self, page_size: int = DEFAULT_PAGE_SIZE):
        super().__init__()
        self.page_size = page_size

    def fetch(self, since: Optional[datetime] = None) -> Tuple[str, str]:
        query = COCHRANE_QUERY
        if since is not None:
            query += f" AND (FIRST_PDATE:[{since.strftime('%Y-%m-%d')} TO *])"
            logger.info(
                "[%s] Fetching up to %d reviews published since %s",
                self.source_id, self.page_size, since.strftime("%Y-%m-%d"),
            )
        else:
            logger.info(
                "[%s] Fetching up to %d reviews (no date filter)",
                self.source_id, self.page_size,
            )

        params = {
            "query":      query,
            "resultType": "core",
            "format":     "json",
            "pageSize":   self.page_size,
            # sort omitted — space in "FIRST_PDATE desc" encodes as "+"
            # which Europe PMC silently rejects, returning {"version":"6.9"}
        }

        resp = self._get(EPMC_SEARCH_URL, params=params)

        if resp.status_code != 200:
            raise RuntimeError(
                f"[{self.source_id}] Europe PMC returned HTTP {resp.status_code} — "
                f"not writing to Bronze"
            )

        raw = resp.text

        # Europe PMC silently rejects malformed queries with HTTP 200 and a body
        # of just {"version":"6.9"} — no hitCount, no resultList. Guard against
        # writing that stub to Bronze where the normaliser would see zero hits.
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"[{self.source_id}] Response was not valid JSON: {e}"
            ) from e

        if "hitCount" not in data or "resultList" not in data:
            raise RuntimeError(
                f"[{self.source_id}] Response missing 'hitCount'/'resultList' — "
                f"likely a silently-rejected query. Keys: {list(data.keys())}"
            )

        logger.info(
            "[%s] Fetched %d bytes",
            self.source_id, len(raw.encode("utf-8")),
        )
        return raw, self.file_extension