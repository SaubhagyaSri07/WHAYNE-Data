import json
import logging
from datetime import datetime
from typing import Optional, Tuple

from bs4 import BeautifulSoup

from adapters.base import BaseAdapter

logger = logging.getLogger(__name__)

CDSCO_BASE_URL   = "https://cdscomdonline.gov.in"
CDSCO_PORTAL_URL = f"{CDSCO_BASE_URL}/NewMedDev/ListOfApprovedDevices"
CDSCO_API_URL    = f"{CDSCO_BASE_URL}/NewMedDev/viewListOfApprovedDevices"

# selectFormType values
FORM_MANUFACTURER = 2
FORM_IMPORT       = 1
CATEG_VAL         = 1


class CDSCODevicesAdapter(BaseAdapter):
    """
    Fetches approved medical devices from the CDSCO (India) internal AJAX API.
    Stores the raw combined JSON to Bronze — zero processing.

    The portal page renders via JavaScript; the underlying data is served
    by a separate AJAX endpoint discovered by inspecting network traffic.
    We establish a session first (for the session cookie + CSRF token),
    then call the API directly — no Playwright needed.

    Two categories are fetched per run:
      selectFormType=2  →  Manufacturer approvals
      selectFormType=1  →  Import approvals

    Raw Bronze format:
      {
        "manufacturer": { "iTotalRecords": N, "aaData": [...] },
        "import":       { "iTotalRecords": N, "aaData": [...] },
        "fetched_at":   "ISO timestamp"
      }

    # ── TODO: Watermarking ────────────────────────────────────────────────
    # The CDSCO API returns no approval dates, so standard date-based
    # watermarking is not possible.  Instead, track seen license numbers:
    #   known_ids = WatermarkStore.get_set(self.source_id)
    #   new_records = [r for r in aaData if r["str_licence_no"] not in known_ids]
    # After each run: WatermarkStore.add_ids(self.source_id, new_license_nos)
    # ─────────────────────────────────────────────────────────────────────
    """

    source_id:        str   = "cdsco"
    source_category:  str   = "regulatory"
    version:          str   = "1.0.0"
    file_extension:   str   = "json"
    rate_limit_delay: float = 2.0   # CDSCO is rate-sensitive

    def fetch(self, since: Optional[datetime] = None) -> Tuple[str, str]:
        """
        Establish session, fetch both categories, return combined raw JSON.

        Args:
            since: reserved for future ID-based watermarking (see TODO above).
                   The CDSCO API has no date filter; ignored for now.
        """
        csrf_token = self._establish_session()

        mfr_data = self._fetch_category(FORM_MANUFACTURER, CATEG_VAL, csrf_token, "Manufacturer")
        imp_data = self._fetch_category(FORM_IMPORT,       CATEG_VAL, csrf_token, "Import")

        combined = json.dumps({
            "manufacturer": mfr_data,
            "import":       imp_data,
            "fetched_at":   datetime.utcnow().isoformat(),
        }, ensure_ascii=False)

        total = (
            len((mfr_data or {}).get("aaData", [])) +
            len((imp_data or {}).get("aaData", []))
        )
        logger.info(
            "[%s] Combined %d records (%d bytes)",
            self.source_id, total, len(combined.encode("utf-8")),
        )
        return combined, self.file_extension

    def _establish_session(self) -> Optional[str]:
        """
        Visit the portal page to set the session cookie and extract
        the CSRF token from <meta name="_csrf" content="...">.
        """
        try:
            resp = self._get(
                CDSCO_PORTAL_URL,
                headers={
                    "Accept":          "text/html,application/xhtml+xml",
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
            soup  = BeautifulSoup(resp.content, "lxml")
            meta  = soup.find("meta", {"name": "_csrf"})
            token = meta["content"] if meta else None

            if token:
                logger.info("[%s] Session established, CSRF token obtained", self.source_id)
            else:
                logger.warning(
                    "[%s] No CSRF token found — proceeding without", self.source_id
                )
            return token

        except Exception as e:
            logger.error("[%s] Session setup failed: %s", self.source_id, e)
            return None

    def _fetch_category(
        self,
        select_form_type: int,
        categ_val:        int,
        csrf_token:       Optional[str],
        label:            str,
    ) -> Optional[dict]:
        """Call the AJAX endpoint for one form type and return the raw dict."""
        headers = {
            "Accept":           "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Referer":          CDSCO_PORTAL_URL,
        }
        if csrf_token:
            headers["X-CSRF-TOKEN"] = csrf_token

        params = {
            "selectFormType": select_form_type,
            "categval":       categ_val,
        }

        try:
            resp = self._get(CDSCO_API_URL, params=params, headers=headers)
            data = resp.json()
            logger.info(
                "[%s] %s: %d records (total reported: %d)",
                self.source_id, label,
                len(data.get("aaData", [])),
                data.get("iTotalRecords", 0),
            )
            return data

        except Exception as e:
            logger.error("[%s] %s fetch failed: %s", self.source_id, label, e)
            return {"iTotalRecords": 0, "aaData": []}