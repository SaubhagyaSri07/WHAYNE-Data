import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from schema import Actor, ActorRole, CanonicalRecord, EntityType, Lineage

logger = logging.getLogger(__name__)

FDA_PMN_URL = "https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfpmn/pmn.cfm?ID={}"

# decision_code → human-readable label
DECISION_LABELS = {
    "SESE": "Substantially Equivalent",
    "SESP": "Substantially Equivalent — With Special Conditions",
    "DENG": "Deemed Not to be a 510(k)",
    "NSE":  "Not Substantially Equivalent",
}


class FDA510KMap:
    """
    Maps raw openFDA 510(k) JSON to CanonicalRecord objects.

    Input:  raw JSON string from Bronze (full openFDA response envelope)
    Output: list of CanonicalRecord

    openFDA quirk: openfda sub-object fields have mixed types —
    some are plain strings (device_name, medical_specialty_description,
    regulation_number, device_class), others are lists (registration_number,
    fei_number).  _openfda_val() handles both transparently.
    """

    source_id:       str = "fda_510k"
    source_category: str = "regulatory"
    version:         str = "1.0.0"

    def normalise(self, raw_json: str) -> List[CanonicalRecord]:
        if not raw_json or not raw_json.strip():
            logger.warning("[%s] Empty raw JSON — nothing to normalise", self.source_id)
            return []

        try:
            payload = json.loads(raw_json)
        except json.JSONDecodeError as e:
            logger.error("[%s] JSON parse error: %s", self.source_id, e)
            return []

        results = payload.get("results", [])
        logger.info("[%s] Normalising %d records", self.source_id, len(results))

        records, skipped = [], 0
        for item in results:
            try:
                record = self._map_record(item)
                if record:
                    records.append(record)
                else:
                    skipped += 1
            except Exception as e:
                k_num = item.get("k_number", "unknown")
                logger.warning(
                    "[%s] Record mapping failed (%s): %s",
                    self.source_id, k_num, e,
                )
                skipped += 1

        logger.info(
            "[%s] Normalised %d records (%d skipped)",
            self.source_id, len(records), skipped,
        )
        return records

    def _map_record(self, r: Dict[str, Any]) -> Optional[CanonicalRecord]:
        # ── K number (required) ───────────────────────────────────────────
        k_number = r.get("k_number", "").strip()
        if not k_number:
            return None

        # ── Title (device name, required) ─────────────────────────────────
        title = r.get("device_name", "").strip()
        if not title:
            return None

        # ── Published at (decision date) ──────────────────────────────────
        published_at = self._parse_date(r.get("decision_date", ""))

        # ── Actor: applicant company ──────────────────────────────────────
        actors = []
        applicant_name = r.get("applicant", "").strip()
        if applicant_name:
            address_parts = [
                r.get("address_1", "").strip(),
                r.get("city",      "").strip(),
                r.get("state",     "").strip(),
            ]
            address = ", ".join(p for p in address_parts if p) or None
            actors.append(Actor(
                name    = applicant_name,
                role    = ActorRole.APPLICANT,
                address = address,
                country = r.get("country_code", "").strip() or None,
            ))

        # ── openFDA metadata ──────────────────────────────────────────────
        openfda = r.get("openfda", {})

        medical_specialty = self._openfda_val(openfda, "medical_specialty_description")
        device_class      = self._openfda_val(openfda, "device_class")
        regulation_number = self._openfda_val(openfda, "regulation_number")

        # ── Decision label ────────────────────────────────────────────────
        decision_code  = r.get("decision_code", "").strip()
        decision_label = DECISION_LABELS.get(decision_code, decision_code)

        # ── Tags ──────────────────────────────────────────────────────────
        # CanonicalRecord validator lowercases + deduplicates automatically
        tags = [t for t in [title, medical_specialty] if t]

        # ── Classifiers ───────────────────────────────────────────────────
        classifiers = {
            "k_number":          k_number,
            "decision_code":     decision_code,
            "decision":          decision_label,
            "product_code":      r.get("product_code",        ""),
            "clearance_type":    r.get("clearance_type",      ""),
            "date_received":     r.get("date_received",       ""),
            "device_class":      device_class,
            "medical_specialty": medical_specialty,
            "regulation_number": regulation_number,
            "third_party_flag":  r.get("third_party_flag",    ""),
            "expedited_review":  r.get("expedited_review_flag", ""),
        }

        return CanonicalRecord(
            source_id    = self.source_id,
            source_type  = self.source_category,
            source_url   = FDA_PMN_URL.format(k_number),
            external_id  = k_number,
            published_at = published_at,
            entity_type  = EntityType.DEVICE_CLEARANCE,
            title        = title,
            summary      = None,   # not in openFDA 510(k) endpoint
            region       = r.get("country_code", "").strip() or None,
            language     = "en",
            actors       = actors,
            tags         = tags,
            classifiers  = classifiers,
            lineage = Lineage(
                adapter_version = self.version,
                pipeline_run_id = datetime.utcnow().isoformat(),
                llm_assisted    = False,
            ),
        )

    @staticmethod
    def _openfda_val(openfda: dict, key: str) -> str:
        """
        Safely extract a value from the openFDA sub-object.

        openFDA fields have inconsistent types across endpoints:
        - Some are plain strings (medical_specialty_description, device_class)
        - Some are lists       (registration_number, fei_number)
        This helper returns the scalar value regardless of which type is used.
        """
        val = openfda.get(key)
        if val is None:
            return ""
        if isinstance(val, list):
            return str(val[0]).strip() if val else ""
        return str(val).strip()

    @staticmethod
    def _parse_date(date_str: str) -> Optional[datetime]:
        """Parse openFDA date strings (YYYY-MM-DD)."""
        if not date_str:
            return None
        try:
            return datetime.strptime(date_str.strip(), "%Y-%m-%d")
        except ValueError:
            return None