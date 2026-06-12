import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional, Tuple

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)


class BaseAdapter(ABC):
    """
    Base class for all source adapters.

    Each adapter is responsible for exactly one thing:
    fetching raw data from its source and returning it
    as an unmodified string along with its file extension.

    The adapter does NOT parse, transform, or normalise.
    That is the normaliser's job.
    """

    source_id:        str
    source_category:  str
    version:          str   = "1.0.0"
    rate_limit_delay: float = 1.0

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "MarketIntelligenceBot/1.0 (research; contact@example.com)"
        })
        self._last_request_time: float = 0.0

    def _throttle(self):
        elapsed = time.time() - self._last_request_time
        if elapsed < self.rate_limit_delay:
            time.sleep(self.rate_limit_delay - elapsed)
        self._last_request_time = time.time()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10)
    )
    def _get(
        self,
        url:     str,
        params:  Optional[dict] = None,
        headers: Optional[dict] = None,
    ) -> requests.Response:
        self._throttle()
        logger.debug(f"[{self.source_id}] GET {url} params={params}")
        response = self.session.get(
            url, params=params, headers=headers, timeout=30
        )
        if response.status_code == 404:
            return response
        response.raise_for_status()
        return response

    @abstractmethod
    def fetch(self, since: Optional[datetime] = None) -> Tuple[str, str]:
        """
        Fetch raw data from the source.

        Returns:
            Tuple of (raw_content_string, file_extension)
            e.g. ('<PubmedArticleSet>...', 'xml')
            e.g. ('{"results": [...]}', 'json')

        The raw_content_string is written to Bronze exactly as returned.
        No parsing, no transformation, no field extraction.
        """
        pass