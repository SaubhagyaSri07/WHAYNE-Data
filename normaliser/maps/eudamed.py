import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from schema import Actor, ActorRole, CanonicalRecord, EntityType, Lineage

logger = logging.getLogger(__name__)

EUDAMED_DEVICE_URL = "https://ec.europa.eu/tools/eudamed/#/screen/device/{uuid}"
EUDAMED_BASE_URL   = "https://ec.europa.eu/tools/eudamed/"

# Decode EUDAMED refdata code strings to short labels
# e.g. "refdata.risk-class.class-iia" → "class-iia"
def _code(raw: Optional[dict]) -> str:
    if not raw:
        return ""
    code_str = raw.get("code", "")
    parts = code_str.split(".")
    return parts[-1] if parts else code_str


class EUDAMEDMap:
    """
    Maps raw EUDAMED udiDiData JSON to CanonicalRecord objects.

    Input:  Bronze JSON from /api/devices/udiDiData:
              { "content": [...], "totalElements": N, "totalPages": N }
    Output: list of CanonicalRecord (entity_type = DEVICE_CLEARANCE)

    Each record is a UDI-DI (device identifier) registration —
    a medical device placed on the EU market under MDR or IVDR.

    Note: tradeName can be in any language (Chinese, German, etc.).
    deviceName and deviceModel are null for most records in this endpoint.
    """

    source_id:       str = "eudamed"
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

        content        = payload.get("content", [])
        total_elements = payload.get("totalElements", 0)
        logger.info(
            "[%s] Normalising %d devices (total available: %d)",
            self.source_id, len(content), total_elements,
        )

        records, skipped = [], 0
        for item in content:
            try:
                record = self._map_device(item)
                if record:
                    records.append(record)
                else:
                    skipped += 1
            except Exception as e:
                pid = item.get("primaryDi", "unknown")
                logger.warning(
                    "[%s] Device mapping failed (%s): %s",
                    self.source_id, pid, e,
                )
                skipped += 1

        logger.info(
            "[%s] Normalised %d records (%d skipped)",
            self.source_id, len(records), skipped,
        )
        return records

    def _map_device(self, r: Dict[str, Any]) -> Optional[CanonicalRecord]:
        # ── Primary DI (required) ─────────────────────────────────────────
        primary_di = (r.get("primaryDi") or "").strip()
        if not primary_di:
            return None

        # ── Trade name → title ────────────────────────────────────────────
        trade_name   = (r.get("tradeName")    or "").strip()
        device_name  = (r.get("deviceName")   or "").strip()
        device_model = (r.get("deviceModel")  or "").strip()
        title = trade_name or device_name or primary_di
        if not title:
            return None

        # ── Identifiers ───────────────────────────────────────────────────
        uuid     = (r.get("uuid")     or "").strip()
        ulid     = (r.get("ulid")     or "").strip()
        basic_udi = (r.get("basicUdi") or "").strip()

        # ── Manufacturer ──────────────────────────────────────────────────
        mfr_name = (r.get("manufacturerName") or "").strip()
        mfr_srn  = (r.get("manufacturerSrn")  or "").strip()

        # ── Authorised representative ─────────────────────────────────────
        ar_name = (r.get("authorisedRepresentativeName") or "").strip()
        ar_srn  = (r.get("authorisedRepresentativeSrn")  or "").strip()

        # ── Decoded codes ─────────────────────────────────────────────────
        risk_class    = _code(r.get("riskClass"))
        device_status = _code(r.get("deviceStatusType"))
        legislation   = _code(r.get("applicableLegislation"))

        # ── Region from manufacturer SRN ──────────────────────────────────
        # Format: CC-MF-000000000 → "CC"
        region = mfr_srn.split("-")[0] if mfr_srn and "-" in mfr_srn else None

        # ── Actors ────────────────────────────────────────────────────────
        actors = []
        if mfr_name:
            actors.append(Actor(
                name    = mfr_name,
                role    = ActorRole.APPLICANT,
                country = region,
            ))
        if ar_name:
            # Authorised representative = the EU-responsible entity for
            # non-EU manufacturers. Closest schema role is APPLICANT.
            actors.append(Actor(
                name = ar_name,
                role = ActorRole.MANUFACTURER,
            ))

        # ── Tags — validator lowercases + deduplicates ────────────────────
        tags = [t for t in [risk_class, legislation] if t]

        # ── Classifiers ───────────────────────────────────────────────────
        classifiers = {
            "primary_di":       primary_di,
            "basic_udi":        basic_udi,
            "risk_class":       risk_class,
            "device_status":    device_status,
            "legislation":      legislation,
            "manufacturer_srn": mfr_srn,
            "ar_srn":           ar_srn,
            "reference":        (r.get("reference") or "").strip(),
            "version_number":   r.get("versionNumber", 1),
            "ulid":             ulid,
        }

        # ── Source URL ────────────────────────────────────────────────────
        source_url = (
            EUDAMED_DEVICE_URL.format(uuid=uuid) if uuid else EUDAMED_BASE_URL
        )

        return CanonicalRecord(
            source_id    = self.source_id,
            source_type  = self.source_category,
            source_url   = source_url,
            external_id  = primary_di,
            published_at = None,   # no date available in this endpoint
            entity_type  = EntityType.DEVICE_CLEARANCE,
            title        = title,
            summary      = None,
            region       = region,
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