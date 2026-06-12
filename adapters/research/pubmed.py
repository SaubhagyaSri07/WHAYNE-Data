import logging
from datetime import datetime, timedelta
from typing import Optional, Tuple

from adapters.base import BaseAdapter

logger = logging.getLogger(__name__)

ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH_URL  = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

# Default search — medical device related publications
# Can be overridden when calling fetch()
DEFAULT_QUERY = (
    "medical device[MeSH Terms] OR "
    "diagnostic equipment[MeSH Terms] OR "
    "surgical instruments[MeSH Terms]"
)


class PubMedAdapter(BaseAdapter):
    """
    Fetches research papers from PubMed via NCBI E-utilities.

    Flow:
      1. esearch  — search by query + date range → returns list of PMIDs
      2. efetch   — fetch full XML records for those PMIDs

    Bronze output: raw PubMed XML exactly as returned by NCBI.
    No parsing happens here.
    """

    source_id:        str   = "pubmed"
    source_category:  str   = "research"
    rate_limit_delay: float = 0.4   # NCBI allows 3 req/sec without API key

    def fetch(
        self,
        since:  Optional[datetime] = None,
        query:  str                = DEFAULT_QUERY,
        limit:  int                = 20,
    ) -> Tuple[str, str]:
        """
        Returns raw PubMed XML string and extension 'xml'.
        """
        if since is None:
            since = datetime.utcnow() - timedelta(days=30)

        # ── Step 1: Search for PMIDs ──────────────────────────────────────
        pmids = self._search(query=query, since=since, limit=limit)

        if not pmids:
            logger.info(f"[{self.source_id}] No PMIDs found for query")
            return ("", "xml")

        logger.info(f"[{self.source_id}] Found {len(pmids)} PMIDs")

        # ── Step 2: Fetch full XML records ────────────────────────────────
        raw_xml = self._fetch_records(pmids)
        logger.info(
            f"[{self.source_id}] Fetched {len(raw_xml)} bytes of raw XML"
        )

        return (raw_xml, "xml")

    def _search(
        self,
        query: str,
        since: datetime,
        limit: int,
    ) -> list:
        """Run esearch and return list of PMIDs."""
        since_str = since.strftime("%Y/%m/%d")
        today_str = datetime.utcnow().strftime("%Y/%m/%d")

        params = {
            "db":      "pubmed",
            "term":    f"{query} AND ({since_str}[PDAT]:{today_str}[PDAT])",
            "retmax":  limit,
            "retmode": "json",
            "sort":    "pub+date",
        }

        try:
            response = self._get(ESEARCH_URL, params=params)
            if response.status_code != 200:
                logger.error(
                    f"[{self.source_id}] esearch returned {response.status_code}"
                )
                return []
            data  = response.json()
            pmids = data.get("esearchresult", {}).get("idlist", [])
            return pmids
        except Exception as e:
            logger.error(f"[{self.source_id}] esearch failed: {e}")
            return []

    def _fetch_records(self, pmids: list) -> str:
        """Run efetch and return raw XML string."""
        params = {
            "db":      "pubmed",
            "id":      ",".join(pmids),
            "rettype": "xml",
            "retmode": "xml",
        }

        try:
            response = self._get(EFETCH_URL, params=params)
            if response.status_code != 200:
                logger.error(
                    f"[{self.source_id}] efetch returned {response.status_code}"
                )
                return ""
            return response.text
        except Exception as e:
            logger.error(f"[{self.source_id}] efetch failed: {e}")
            return ""