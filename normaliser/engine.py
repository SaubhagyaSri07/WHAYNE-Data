import logging
from typing import List

from normaliser.maps.clinicaltrials import ClinicalTrialsMap
from normaliser.maps.pubmed import PubMedMap
from schema import CanonicalRecord
from normaliser.maps.fda_510k import FDA510KMap
from normaliser.maps.cdsco import CDSCODevicesMap
from normaliser.maps.rss import RSSMap
from normaliser.maps.cochrane import CochraneMap
from normaliser.maps.eudamed import EUDAMEDMap
from normaliser.maps.uspto import USPTOMap
from normaliser.maps.comtrade import ComtradeMap
from normaliser.maps.google_patents import GooglePatentsMap

logger = logging.getLogger(__name__)


class NormaliserEngine:
    """
    Routes raw Bronze content to the correct source map,
    validates output against the canonical schema,
    and returns a list of CanonicalRecord objects ready for Silver.

    To add a new source:
      1. Create normaliser/maps/<source_id>.py with a Map class
      2. Import it here and add to self._maps
    """

    def __init__(self):
        self._maps = {
            "pubmed":         PubMedMap(),
            "clinicaltrials": ClinicalTrialsMap(),
            "fda_510k": FDA510KMap(),
            # Added as each source is built:
            "cdsco": CDSCODevicesMap(),
            "eudamed": EUDAMEDMap(),
            # "epo":       EPOMap(),
            "uspto": USPTOMap(),
            "comtrade": ComtradeMap(),
            "rss": RSSMap(),
            "cochrane": CochraneMap(),
            "google_patents": GooglePatentsMap(),
        }

    def normalise(self, source_id: str, raw: str) -> List[CanonicalRecord]:
        """
        Normalise raw Bronze content for a given source.

        Args:
            source_id: e.g. "pubmed", "clinicaltrials"
            raw:       raw content string from Bronze

        Returns:
            List of validated CanonicalRecord objects.
            Empty list if source has no map or normalisation fails.
        """
        map_handler = self._maps.get(source_id)
        if not map_handler:
            logger.error(
                "[normaliser] No map registered for '%s'. Available: %s",
                source_id, list(self._maps.keys()),
            )
            return []

        logger.info("[normaliser] Running map for %s", source_id)

        try:
            records = map_handler.normalise(raw)
            logger.info(
                "[normaliser] %s → %d canonical records",
                source_id, len(records),
            )
            return records
        except Exception as e:
            logger.error("[normaliser] %s map failed: %s", source_id, e)
            return []

    @property
    def registered_sources(self) -> List[str]:
        return list(self._maps.keys())