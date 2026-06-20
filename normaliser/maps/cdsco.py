import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from schema import Actor, ActorRole, CanonicalRecord, EntityType, Lineage

logger = logging.getLogger(__name__)

CDSCO_PORTAL_URL = "https://cdscomdonline.gov.in/NewMedDev/ListOfApprovedDevices"


class CDSCODevicesMap:
    """
    Maps raw CDSCO approved-devices JSON to CanonicalRecord objects.

    Input:  combined Bronze JSON:
              { "manufacturer": { "aaData": [...] },
                "import":       { "aaData": [...] } }
    Output: list of CanonicalRecord

    Raw field quirks handled here (not in the adapter):
      str_licence_no  — multiline: "Licence No: {num}\\nIssed On: {date}"
                        Note CDSCO typo: "Issed On" not "Issued On"
      address         — multiline: "{company name}\\n{street address}"
    """

    source_id:       str = "cdsco"
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

        records, skipped = [], 0

        for category in ("manufacturer", "import"):
            items = payload.get(category, {}).get("aaData", [])
            logger.info(
                "[%s] Normalising %d %s records",
                self.source_id, len(items), category,
            )
            for item in items:
                try:
                    record = self._map_record(item, category)
                    if record:
                        records.append(record)
                    else:
                        skipped += 1
                except Exception as e:
                    lic = item.get("str_licence_no", "unknown")
                    logger.warning(
                        "[%s] Record mapping failed (%s): %s",
                        self.source_id, lic, e,
                    )
                    skipped += 1

        logger.info(
            "[%s] Normalised %d records (%d skipped)",
            self.source_id, len(records), skipped,
        )
        return records

    def _map_record(self, r: Dict[str, Any], category: str) -> Optional[CanonicalRecord]:
        # ── Device name (required) ────────────────────────────────────────
        device_name = (r.get("devicename") or "").strip()
        if not device_name:
            return None

        # ── License number + issued date ──────────────────────────────────
        license_raw = (r.get("str_licence_no") or "").strip()
        license_no, issued_date = self._parse_license(license_raw)

        # ── External ID: stable, unique per device approval ───────────────
        # Neither formid nor permissionid is globally unique; the
        # combination of license number + device name is.
        external_id = f"{license_no}:{device_name}" if license_no else None

        # ── Actor: manufacturer or importer ───────────────────────────────
        actors = []
        address_raw = (r.get("address") or "").strip()
        if address_raw:
            company_name, street_address = self._parse_address(address_raw)
            role = ActorRole.MANUFACTURER if category == "manufacturer" else ActorRole.IMPORTER
            actors.append(Actor(
                name    = company_name or address_raw[:300],
                role    = role,
                address = street_address or None,
                country = "IN",
            ))

        # ── Other fields ──────────────────────────────────────────────────
        device_class  = (r.get("classname")        or "").strip()
        intended_use  = (r.get("str_intended_use") or "").strip()
        brand_name    = (r.get("brandname")        or "").strip()
        model_numbers = (r.get("modelname")        or "").strip()
        issuing_auth  = (r.get("instname")         or "").strip()

        # ── Tags — validator lowercases + deduplicates ────────────────────
        tags = [t for t in [device_name, device_class, brand_name] if t]

        # ── Summary ───────────────────────────────────────────────────────
        summary_parts = [p for p in [
            intended_use[:300]                   if intended_use else "",
            f"Class: {device_class}"             if device_class else "",
            f"Brand: {brand_name}"               if brand_name   else "",
            f"Issuing Authority: {issuing_auth}" if issuing_auth else "",
        ] if p]
        summary = " | ".join(summary_parts) or None

        # ── Classifiers ───────────────────────────────────────────────────
        classifiers = {
            "license_no":        license_no,
            "issued_on":         issued_date.strftime("%Y-%m-%d") if issued_date else "",
            "device_class":      device_class,
            "brand_name":        brand_name,
            "model_numbers":     model_numbers[:500] if model_numbers else "",
            "issuing_authority": issuing_auth,
            "intended_use":      intended_use[:500]  if intended_use else "",
            "category":          category,
        }

        return CanonicalRecord(
            source_id    = self.source_id,
            source_type  = self.source_category,
            source_url   = CDSCO_PORTAL_URL,
            external_id  = external_id,
            published_at = issued_date,
            entity_type  = EntityType.DEVICE_CLEARANCE,
            title        = device_name,
            summary      = summary,
            region       = "IN",
            language     = "en",
            actors       = actors,
            tags         = tags,
            classifiers  = classifiers,
            lineage = Lineage(
                adapter_version = self.version,
                pipeline_run_id = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                llm_assisted    = False,
            ),
        )

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _parse_license(raw: str) -> Tuple[str, Optional[datetime]]:
        """
        Parse 'Licence No: 02/SCof2018\\nIssed On: 09-JAN-2018'
        into (license_number, issued_datetime).

        Handles CDSCO's consistent typo "Issed On" instead of "Issued On".
        """
        license_no  = ""
        issued_date = None

        for line in raw.splitlines():
            line = line.strip()
            if line.lower().startswith("licence no:"):
                license_no = line.split(":", 1)[1].strip()
            elif line.lower().startswith("iss"):   # "Issed On:" or "Issued On:"
                date_str = line.split(":", 1)[1].strip()
                try:
                    issued_date = datetime.strptime(date_str, "%d-%b-%Y")
                except ValueError:
                    pass

        return license_no, issued_date

    @staticmethod
    def _parse_address(raw: str) -> Tuple[str, str]:
        """
        Split 'Company Name\\nStreet address, City, State...'
        into (company_name, street_address).
        CDSCO address fields consistently use the first line as company name.
        """
        lines = [l.strip() for l in raw.splitlines() if l.strip()]
        if not lines:
            return "", ""
        company_name   = lines[0]
        street_address = " ".join(lines[1:]) if len(lines) > 1 else ""
        return company_name, street_address