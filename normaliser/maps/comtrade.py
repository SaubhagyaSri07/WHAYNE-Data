import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from schema import Actor, ActorRole, CanonicalRecord, EntityType, Lineage

logger = logging.getLogger(__name__)

COMTRADE_URL = "https://comtradeapi.un.org/public/v1/preview/C/A/HS"

FLOW_LABELS = {"X": "Export", "M": "Import"}

# ── M49 numeric → country name lookup ────────────────────────────────────
# ISO 3166-1 numeric codes (= UN M49 codes for countries).
# Special codes: 0=World, 899=Other/unspecified, 471=EU
M49_COUNTRIES: Dict[int, str] = {
    0:   "World",
    4:   "Afghanistan",
    8:   "Albania",
    12:  "Algeria",
    20:  "Andorra",
    24:  "Angola",
    28:  "Antigua and Barbuda",
    32:  "Argentina",
    36:  "Australia",
    40:  "Austria",
    51:  "Armenia",
    31:  "Azerbaijan",
    44:  "Bahamas",
    48:  "Bahrain",
    50:  "Bangladesh",
    52:  "Barbados",
    112: "Belarus",
    56:  "Belgium",
    84:  "Belize",
    204: "Benin",
    64:  "Bhutan",
    68:  "Bolivia",
    70:  "Bosnia and Herzegovina",
    72:  "Botswana",
    76:  "Brazil",
    96:  "Brunei",
    100: "Bulgaria",
    854: "Burkina Faso",
    108: "Burundi",
    116: "Cambodia",
    120: "Cameroon",
    124: "Canada",
    132: "Cabo Verde",
    140: "Central African Republic",
    148: "Chad",
    152: "Chile",
    156: "China",
    170: "Colombia",
    174: "Comoros",
    178: "Congo",
    180: "Congo, Democratic Republic",
    188: "Costa Rica",
    384: "Cote d'Ivoire",
    191: "Croatia",
    192: "Cuba",
    196: "Cyprus",
    203: "Czech Republic",
    208: "Denmark",
    262: "Djibouti",
    214: "Dominican Republic",
    218: "Ecuador",
    818: "Egypt",
    222: "El Salvador",
    231: "Ethiopia",
    238: "Falkland Islands",
    246: "Finland",
    250: "France",
    266: "Gabon",
    270: "Gambia",
    268: "Georgia",
    276: "Germany",
    288: "Ghana",
    300: "Greece",
    320: "Guatemala",
    324: "Guinea",
    624: "Guinea-Bissau",
    332: "Haiti",
    340: "Honduras",
    344: "Hong Kong",
    348: "Hungary",
    356: "India",
    360: "Indonesia",
    364: "Iran",
    368: "Iraq",
    372: "Ireland",
    376: "Israel",
    380: "Italy",
    388: "Jamaica",
    392: "Japan",
    400: "Jordan",
    398: "Kazakhstan",
    404: "Kenya",
    408: "Korea (North)",
    410: "Korea (South)",
    414: "Kuwait",
    418: "Laos",
    422: "Lebanon",
    430: "Liberia",
    434: "Libya",
    442: "Luxembourg",
    454: "Malawi",
    458: "Malaysia",
    462: "Maldives",
    466: "Mali",
    484: "Mexico",
    496: "Mongolia",
    504: "Morocco",
    508: "Mozambique",
    516: "Namibia",
    524: "Nepal",
    528: "Netherlands",
    554: "New Zealand",
    566: "Nigeria",
    578: "Norway",
    512: "Oman",
    586: "Pakistan",
    591: "Panama",
    600: "Paraguay",
    604: "Peru",
    608: "Philippines",
    616: "Poland",
    620: "Portugal",
    634: "Qatar",
    642: "Romania",
    643: "Russia",
    646: "Rwanda",
    682: "Saudi Arabia",
    686: "Senegal",
    694: "Sierra Leone",
    703: "Slovakia",
    705: "Slovenia",
    706: "Somalia",
    710: "South Africa",
    724: "Spain",
    144: "Sri Lanka",
    729: "Sudan",
    752: "Sweden",
    756: "Switzerland",
    760: "Syria",
    158: "Taiwan",
    764: "Thailand",
    768: "Togo",
    780: "Trinidad and Tobago",
    788: "Tunisia",
    792: "Turkey",
    800: "Uganda",
    804: "Ukraine",
    784: "United Arab Emirates",
    826: "United Kingdom",
    840: "United States",
    842: "United States (incl. territories)",
    858: "Uruguay",
    860: "Uzbekistan",
    862: "Venezuela",
    704: "Vietnam",
    887: "Yemen",
    894: "Zambia",
    716: "Zimbabwe",
    251: "France (incl. Monaco)",
    688: "Serbia",
    757: "Switzerland, Liechtenstein",
    531: "Curaçao",
    807: "North Macedonia",
    428: "Latvia",
    440: "Lithuania",
    498: "Moldova",
    702: "Singapore",
    699: "India (alt)",
    579: "Norway (incl. Svalbard)",
    381: "Italy (incl. San Marino)",
    757: "Switzerland, Liechtenstein",
    499: "Montenegro",
    795: "Turkmenistan",
    233: "Estonia",
    470: "Malta",
    834: "Tanzania",
    417: "Kyrgyzstan",
    104: "Myanmar",
    480: "Mauritius",
    762: "Tajikistan",
    837: "Kosovo",
    558: "Nicaragua",
    136: "Cayman Islands",
    328: "Guyana",
    352: "Iceland",
    275: "Palestine",
    446: "Macao",
    540: "New Caledonia",
    # Special/aggregate codes
    471: "European Union",
    899: "Other/Unspecified",
    579: "Norway (incl. Svalbard)",
    490: "Other Asia",
    
}


def _country_name(code: Optional[int]) -> str:
    """Resolve M49 numeric code to country name."""
    if code is None:
        return "World"
    return M49_COUNTRIES.get(code, f"Country {code}")


class ComtradeMap:
    """
    Maps raw UN Comtrade preview JSON to CanonicalRecord objects.

    Input:  Bronze envelope:
              { "period": "2024", "queries": [
                  { "cmd_code": "9018", "cmd_desc": "...",
                    "flow_code": "X", "records": [...] }, ... ] }
    Output: list of CanonicalRecord (entity_type = TRADE_SHIPMENT)

    M49 numeric country codes are resolved to names via the embedded
    lookup table — no Gold-layer enrichment needed.
    """

    source_id:       str = "comtrade"
    source_category: str = "trade"
    version:         str = "1.0.0"

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

        # ── Resolve country names ─────────────────────────────────────────
        reporter_name = _country_name(reporter_code)
        partner_name  = _country_name(partner_code)

        # ISO text from API (null in free tier — kept as fallback if ever populated)
        reporter_iso = (r.get("reporterISO") or "").strip() or str(reporter_code)
        partner_iso  = (r.get("partnerISO")  or "").strip() or str(partner_code or 0)

        # ── Flow ──────────────────────────────────────────────────────────
        fc        = (r.get("flowCode") or flow_code).strip()
        flow_desc = (r.get("flowDesc") or "").strip() or FLOW_LABELS.get(fc, fc)

        # ── Trade values ──────────────────────────────────────────────────
        primary_value = r.get("primaryValue")
        net_wgt       = r.get("netWgt")

        # ── External ID ───────────────────────────────────────────────────
        external_id = f"HS{cmd_code}-{fc}-{reporter_code}-{partner_code}-{period}"

        # ── Title & summary ───────────────────────────────────────────────
        title   = f"HS{cmd_code} {flow_desc}: {reporter_name} → {partner_name} ({period})"
        summary = None
        if primary_value is not None:
            summary = (
                f"{flow_desc} of {cmd_desc} — "
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
            "cmd_desc":          cmd_desc,
            "flow_code":         fc,
            "flow_desc":         flow_desc,
            "reporter_code":     reporter_code,
            "reporter_name":     reporter_name,
            "partner_code":      partner_code,
            "partner_name":      partner_name,
            "period":            period,
            "primary_value_usd": primary_value,
            "net_weight_kg":     net_wgt,
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
                pipeline_run_id = datetime.utcnow().isoformat(),
                llm_assisted    = False,
            ),
        )