import json
import logging
from datetime import datetime
from typing import Optional, Tuple

from adapters.base import BaseAdapter

logger = logging.getLogger(__name__)

EUDAMED_UDI_URL = "https://ec.europa.eu/tools/eudamed/api/devices/udiDiData"
DEFAULT_PAGE_SIZE = 100

# Endpoint discovered via Chrome DevTools network inspection.
# The EUDAMED SPA calls this internal API; all other /api/* paths
# return the Angular shell HTML, not JSON.
#
# ULIDs are time-sortable (first 10 chars = ms-precision timestamp),
# so sort=ulid,desc gives approximately most-recently-registered first.


class EUDAMEDAdapter(BaseAdapter):
    """
    Fetches registered medical devices from the EUDAMED internal API.
    Stores raw JSON response to Bronze — zero processing.

    EUDAMED is the EU medical device registry under MDR/IVDR.
    This endpoint returns UDI-DI (device identifier) records —
    2.4M+ devices with risk class, manufacturer, trade name, and status.
    No authentication required.

    # ── TODO: Watermarking ────────────────────────────────────────────────
    # No explicit date field is available for server-side filtering.
    # Approach: sort by ulid,desc (ULIDs encode registration timestamp).
    # After each run, store the newest ulid seen:
    #   WatermarkStore.set(self.source_id, newest_ulid)
    # Next run: stop processing once an entry's ulid <= stored watermark.
    # This gives incremental ingestion without a date filter parameter.
    # ─────────────────────────────────────────────────────────────────────
    """

    source_id:       str = "eudamed"
    source_category: str = "regulatory"
    version:         str = "1.0.0"
    file_extension:  str = "json"

    def __init__(self, page_size: int = DEFAULT_PAGE_SIZE):
        super().__init__()
        self.page_size = page_size

    def fetch(self, since: Optional[datetime] = None) -> Tuple[str, str]:
        """
        Fetch the most recently registered EUDAMED devices.

        Args:
            since: reserved for ULID-based watermarking (see TODO above).
                   No server-side date filter available; ignored for now.
        """
        params = {
            "page":             0,
            "size":             self.page_size,
            "deviceStatusCode": "refdata.device-model-status.on-the-market",
            "latestVersion":    "true",
            "languageIso2Code": "en",
            "sort":             "ulid,desc",
        }

        logger.info(
            "[%s] Fetching up to %d devices (sort: ulid desc, status: on-the-market)",
            self.source_id, self.page_size,
        )

        resp = self._get(EUDAMED_UDI_URL, params=params)

        if resp.status_code != 200:
            raise RuntimeError(
                f"[{self.source_id}] EUDAMED returned HTTP {resp.status_code} — "
                f"not writing to Bronze"
            )

        raw = resp.text

        # Most EUDAMED /api/* paths return the Angular shell HTML with HTTP 200
        # instead of JSON. Catch that here rather than letting the HTML reach
        # Bronze and break the normaliser downstream.
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"[{self.source_id}] Response was not valid JSON (likely the "
                f"Angular shell HTML — wrong endpoint?): {e}"
            ) from e

        if "content" not in data:
            raise RuntimeError(
                f"[{self.source_id}] Response missing 'content' key — "
                f"unexpected payload shape. Keys: {list(data.keys())}"
            )

        logger.info(
            "[%s] Fetched %d bytes",
            self.source_id, len(raw.encode("utf-8")),
        )
        return raw, self.file_extension