import json
import logging
from datetime import datetime
from typing import List, Optional, Tuple

from adapters.base import BaseAdapter

logger = logging.getLogger(__name__)

# ── Configured feed list ──────────────────────────────────────────────────────
# Only add URLs you have verified return XML (not HTML error pages).
# FDA MedWatch and CDC are confirmed working. The other FDA feeds follow
# the same URL pattern as MedWatch — change the slug to add more:
#   /rss-feeds/medwatch/rss.xml          ← drug/device safety alerts
#   /rss-feeds/medical-device-safety/rss.xml
#   /rss-feeds/drug-safety-communications/rss.xml
#   /rss-feeds/recalls/rss.xml
#   /rss-feeds/press-announcements/rss.xml
#
# To add EMA / WHO / NIH: visit their RSS pages to get current URLs,
# then verify they return XML before adding here.
FEED_URLS: List[str] = [
    # FDA — confirmed working
    "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/medwatch/rss.xml",
    "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/recalls/rss.xml",
    # FDA — same URL pattern, likely working
    "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-announcements/rss.xml",
    "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/drug-safety-communications/rss.xml",
    "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/new-drug-approvals/rss.xml",
    "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/product-approvals/rss.xml",
    # CDC — confirmed working
    "https://tools.cdc.gov/api/v2/resources/media/316422.rss",
]


class RSSAdapter(BaseAdapter):
    """
    Generic RSS/Atom feed adapter.
    Fetches raw XML from each configured feed URL and stores it verbatim
    inside a JSON envelope in Bronze — zero processing.

    Adding a new feed: append its verified URL to FEED_URLS above.
    Feeds that return non-200 (HTML error pages, 403, etc.) are skipped
    and stored with raw_xml=None so they don't corrupt the Bronze file.

    Bronze format:
      {
        "feeds": [
          { "url": "...", "raw_xml": "<?xml ...", "status": 200,
            "fetched_at": "ISO" },
          ...
        ],
        "fetched_at": "ISO"
      }

    # ── TODO: Watermarking ─────────────────────────────────────────────
    # RSS has no server-side date filter.  In the normaliser map, skip
    # entries where published_at < since for client-side filtering.
    # Most feeds return only the latest 20-50 entries anyway so the
    # re-processing volume is small.
    # ──────────────────────────────────────────────────────────────────
    """

    source_id:       str = "rss"
    source_category: str = "news"
    version:         str = "1.0.0"
    file_extension:  str = "json"

    def __init__(self, feed_urls: Optional[List[str]] = None):
        super().__init__()
        self.feed_urls = feed_urls or FEED_URLS

    def fetch(self, since: Optional[datetime] = None) -> Tuple[str, str]:
        run_at = datetime.utcnow().isoformat()
        feeds  = []

        for url in self.feed_urls:
            feeds.append(self._fetch_one(url))

        success = sum(1 for f in feeds if f["raw_xml"])
        logger.info(
            "[%s] Fetched %d/%d feeds successfully",
            self.source_id, success, len(feeds),
        )

        combined = json.dumps(
            {"feeds": feeds, "fetched_at": run_at},
            ensure_ascii=False,
        )
        return combined, self.file_extension

    def _fetch_one(self, url: str) -> dict:
        """
        Fetch one feed URL.  Returns raw_xml=None on any failure so the
        map can skip gracefully without crashing the whole run.

        Critical: only store raw_xml when status == 200.  Non-200
        responses (404 HTML pages, 403 blocks) must not be stored as
        raw_xml or feedparser will try to parse them as XML and fail.
        """
        try:
            resp = self._get(
                url,
                headers={
                    "Accept":     "application/rss+xml, application/atom+xml, text/xml, */*",
                    "User-Agent": "MarketIntelligenceBot/1.0 (research; contact@example.com)",
                },
            )

            # Reject non-200 — servers often return HTML error pages with 404
            # status; feedparser cannot handle these and logs confusing errors.
            if resp.status_code != 200:
                logger.warning(
                    "[%s] %s returned HTTP %d — skipping",
                    self.source_id, url, resp.status_code,
                )
                return {"url": url, "raw_xml": None,
                        "status": resp.status_code,
                        "fetched_at": datetime.utcnow().isoformat()}

            raw_xml = resp.text
            logger.info(
                "[%s] %-70s %d bytes",
                self.source_id, url[:70], len(raw_xml.encode("utf-8")),
            )
            return {
                "url":        url,
                "raw_xml":    raw_xml,
                "status":     resp.status_code,
                "fetched_at": datetime.utcnow().isoformat(),
            }

        except Exception as e:
            logger.warning("[%s] Failed to fetch %s: %s", self.source_id, url, e)
            return {
                "url":        url,
                "raw_xml":    None,
                "status":     None,
                "fetched_at": datetime.utcnow().isoformat(),
            }