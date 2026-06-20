import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from schema import Actor, ActorRole, CanonicalRecord, EntityType, Lineage

logger = logging.getLogger(__name__)

COMTRADE_URL = "https://comtradeapi.un.org/data/v1/get/C/A/HS"

FLOW_LABELS = {"X": "Export", "M": "Import"}

# Defensive fallback only — with includeDesc=true on the Data API,
# reporterDesc/partnerDesc are populated directly by the API and this
# table should rarely be needed. Kept for any edge-case null values.
M49_FALLBACK: Dict[int, str] = {
    0: "World",
}


def _country_name(desc: Optional[str], code: Optional[int]) -> str:
    """Prefer API-provided description; fall back to lookup, then code."""
    if desc:
        return desc.strip()
    if code is None:
        return "World"
    return M49_FALLBACK.get(code, f"Country {code}")


class ComtradeMap:
    """
    Maps raw UN Comtrade Data API JSON to CanonicalRecord objects.

    Input:  Bronze envelope:
              { "period": "2024", "queries": [
                  { "cmd_code": "9018", "cmd_desc": "...",
                    "flow_code": "X", "records": [...] }, ... ] }
    Output: list of CanonicalRecord (entity_type = TRADE_SHIPMENT)

    With includeDesc=true (Basic Individual tier), reporterDesc,
    partnerDesc, cmdDesc, and flowDesc are populated by the API directly —
    no M49 lookup table needed for normal operation.
    """

    source_id:       str = "comtrade"
    source_category: str = "trade"
    version:         str = "2.0.0"

    def normalise(self, raw_json: str) -> List[CanonicalRecord]:
        if not raw_json or not raw_json.strip():
            logger.warning("[%s] Empty raw JSON", self.source_id)
            return []

        try:
            payload = json.loads(raw_json)
        except json.JSONDecodeError as e:
            logger.error("[%s] JSON parse error: %s", self.source_id, e)
            return []

        records, skipped = [], 0
        for query in payload.get("queries", []):
            raw_records = query.get("records", [])
            if not raw_records:
                continue
            cmd_code  = query["cmd_code"]
            cmd_desc  = query.get("cmd_desc", f"HS{cmd_code}")
            flow_code = query["flow_code"]

            for item in raw_records:
                try:
                    record = self._map_record(item, cmd_code, cmd_desc, flow_code)
                    if record:
                        records.append(record)
                    else:
                        skipped += 1
                except Exception as e:
                    logger.warning("[%s] Record mapping failed: %s", self.source_id, e)
                    skipped += 1

        logger.info(
            "[%s] Normalised %d records (%d skipped)",
            self.source_id, len(records), skipped,
        )
        return records

    def _map_record(
        self,
        r: Dict[str, Any],
        cmd_code:  str,
        cmd_desc:  str,
        flow_code: str,
    ) -> Optional[CanonicalRecord]:
        reporter_code = r.get("reporterCode")
        partner_code  = r.get("partnerCode")
        period        = str(r.get("period") or r.get("refYear") or "").strip()

        if reporter_code is None or not period:
            return None

        # ── Names: API-provided (includeDesc=true) with fallback ──────────
        reporter_name = _country_name(r.get("reporterDesc"), reporter_code)
        partner_name  = _country_name(r.get("partnerDesc"),  partner_code)

        # ── ISO codes (may be populated with includeDesc=true) ────────────
        reporter_iso = (r.get("reporterISO") or "").strip() or str(reporter_code)
        partner_iso  = (r.get("partnerISO")  or "").strip() or str(partner_code or 0)

        # ── Flow ──────────────────────────────────────────────────────────
        fc        = (r.get("flowCode") or flow_code).strip()
        flow_desc = (r.get("flowDesc") or "").strip() or FLOW_LABELS.get(fc, fc)

        # ── Commodity description — prefer API-provided ───────────────────
        cmd_description = (r.get("cmdDesc") or "").strip() or cmd_desc

        # ── Trade values ──────────────────────────────────────────────────
        primary_value = r.get("primaryValue")
        net_wgt       = r.get("netWgt")
        qty           = r.get("qty")
        qty_unit      = (r.get("qtyUnitAbbr") or "").strip()

        # ── External ID ───────────────────────────────────────────────────
        external_id = f"HS{cmd_code}-{fc}-{reporter_code}-{partner_code}-{period}"

        # ── Title & summary ───────────────────────────────────────────────
        title   = f"HS{cmd_code} {flow_desc}: {reporter_name} → {partner_name} ({period})"
        summary = None
        if primary_value is not None:
            summary = (
                f"{flow_desc} of {cmd_description} — "
                f"{reporter_name} → {partner_name} ({period}): "
                f"USD {primary_value:,.0f}"
            )

        # ── Published at ──────────────────────────────────────────────────
        try:
            published_at = datetime(int(period), 1, 1)
        except (ValueError, TypeError):
            published_at = None

        # ── Actors ────────────────────────────────────────────────────────
        actors = []
        if fc == "X":
            actors.append(Actor(name=reporter_name, role=ActorRole.EXPORTER,
                                country=reporter_iso))
            actors.append(Actor(name=partner_name,  role=ActorRole.IMPORTER,
                                country=partner_iso))
        else:
            actors.append(Actor(name=reporter_name, role=ActorRole.IMPORTER,
                                country=reporter_iso))
            actors.append(Actor(name=partner_name,  role=ActorRole.EXPORTER,
                                country=partner_iso))

        # ── Tags ──────────────────────────────────────────────────────────
        tags = [f"hs{cmd_code}", flow_desc.lower()]

        # ── Classifiers ───────────────────────────────────────────────────
        classifiers = {
            "cmd_code":          cmd_code,
            "cmd_desc":          cmd_description,
            "flow_code":         fc,
            "flow_desc":         flow_desc,
            "reporter_code":     reporter_code,
            "reporter_name":     reporter_name,
            "reporter_iso":      reporter_iso,
            "partner_code":      partner_code,
            "partner_name":      partner_name,
            "partner_iso":       partner_iso,
            "period":            period,
            "primary_value_usd": primary_value,
            "net_weight_kg":     net_wgt,
            "qty":               qty,
            "qty_unit":          qty_unit,
        }

        return CanonicalRecord(
            source_id    = self.source_id,
            source_type  = self.source_category,
            source_url   = COMTRADE_URL,
            external_id  = external_id,
            published_at = published_at,
            entity_type  = EntityType.TRADE_SHIPMENT,
            title        = title,
            summary      = summary,
            region       = reporter_iso,
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