import json
import logging
from datetime import datetime
from typing import Optional, Tuple

from adapters.base import BaseAdapter

logger = logging.getLogger(__name__)

PATENTSVIEW_URL = "https://api.patentsview.org/patents/query"

# CPC class A61 covers all Medical/Veterinary/Pharma patents:
#   A61B - Diagnosis; surgery
#   A61C - Dentistry
#   A61F - Implants; prostheses
#   A61K - Pharmaceutical preparations
#   A61M - Devices for introducing media into the body
#   A61N - Electrotherapy; magnetotherapy
#   A61P - Therapeutic activity of compounds
#   etc.
CPC_CLASS = "A61"

# Fields to retrieve from PatentsView
FIELDS = [
    "patent_number",
    "patent_title",
    "patent_abstract",
    "patent_date",
    "patent_type",
    "inventors.inventor_first_name",
    "inventors.inventor_last_name",
    "inventors.inventor_country",
    "assignees.assignee_organization",
    "assignees.assignee_country",
    "assignees.assignee_type",
    "cpcs.cpc_section_id",
    "cpcs.cpc_subsection_id",
    "cpcs.cpc_group_id",
    "cpcs.cpc_subgroup_id",
]

DEFAULT_PAGE_SIZE = 100


class USPTOAdapter(BaseAdapter):
    """
    Fetches US medical/pharma patent grants from the PatentsView API.
    Stores raw JSON response to Bronze — zero processing.

    PatentsView is the USPTO's official open-data search API.
    Uses POST (not GET) — BaseAdapter's _get() is not used here;
    _throttle() is called manually before the POST request.

    Filter: CPC class A61 (Medical or Veterinary Science; Hygiene)
    covers all medical device, pharma, diagnostic, and surgical patents.

    # ── TODO: Watermarking ────────────────────────────────────────────────
    # PatentsView supports server-side date filtering:
    #   {"_gte": {"patent_date": "{since}"}}
    # The `since` → date filter wiring is already done below.
    # Implementation steps when ready:
    #   1. Load watermark:  last = WatermarkStore.get(self.source_id)
    #   2. Pass to run():   run(source_id, since=last)
    #   3. After successful run, persist the newest patent_date seen:
    #        WatermarkStore.set(self.source_id, newest_date_seen)
    # ─────────────────────────────────────────────────────────────────────
    """

    source_id:       str = "uspto"
    source_category: str = "patents"
    version:         str = "1.0.0"
    file_extension:  str = "json"

    def __init__(self, page_size: int = DEFAULT_PAGE_SIZE):
        super().__init__()
        self.page_size = page_size

    def fetch(self, since: Optional[datetime] = None) -> Tuple[str, str]:
        """
        Fetch A61 medical/pharma patents and return (raw_json, extension).

        Args:
            since: if provided, filters to patents granted on or after
                   this date using PatentsView's _gte operator.
        """
        # ── Build query ───────────────────────────────────────────────────
        cpc_filter  = {"_eq":  {"cpc_subsection_id": CPC_CLASS}}
        date_filter = (
            {"_gte": {"patent_date": since.strftime("%Y-%m-%d")}}
            if since is not None else None
        )

        if date_filter:
            query_clause = {"_and": [date_filter, cpc_filter]}
            logger.info(
                "[%s] Fetching up to %d A61 patents granted since %s",
                self.source_id, self.page_size, since.strftime("%Y-%m-%d"),
            )
        else:
            query_clause = cpc_filter
            logger.info(
                "[%s] Fetching up to %d A61 patents (no date filter)",
                self.source_id, self.page_size,
            )

        body = {
            "q": query_clause,
            "f": FIELDS,
            "o": {"page": 1, "per_page": self.page_size},
            "s": [{"patent_date": "desc"}],
        }

        # ── POST with manual throttle (BaseAdapter._get is GET-only) ──────
        self._throttle()
        resp = self.session.post(
            PATENTSVIEW_URL,
            json=body,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()

        raw = resp.text
        logger.info(
            "[%s] Fetched %d bytes",
            self.source_id, len(raw.encode("utf-8")),
        )
        return raw, self.file_extension