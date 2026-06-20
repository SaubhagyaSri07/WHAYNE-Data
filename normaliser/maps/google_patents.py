import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from schema import Actor, ActorRole, CanonicalRecord, EntityType, Lineage

logger = logging.getLogger(__name__)

GOOGLE_PATENTS_SOURCE_URL_BASE = "https://patents.google.com/patent/"


def _parse_yyyymmdd(value: Optional[int]) -> Optional[datetime]:
    """BigQuery stores dates as INTEGER YYYYMMDD (e.g. 20250102); 0/None means absent."""
    if not value:
        return None
    try:
        return datetime.strptime(str(int(value)), "%Y%m%d")
    except (ValueError, TypeError):
        return None


def _pick_localized_text(
    items: List[Dict[str, Any]], preferred_lang: str = "en",
) -> Tuple[Optional[str], Optional[str]]:
    """Prefer the given language; fall back to the first non-empty entry.
    Returns (text, language) — both None if nothing usable is present."""
    if not items:
        return None, None
    for it in items:
        if (it.get("language") or "").strip().lower() == preferred_lang:
            text = (it.get("text") or "").strip()
            if text:
                return text, preferred_lang
    for it in items:
        text = (it.get("text") or "").strip()
        if text:
            return text, (it.get("language") or "").strip().lower() or "en"
    return None, None


class GooglePatentsMap:
    """
    Maps raw Google Patents Public Data (BigQuery) JSON to CanonicalRecord
    objects.

    Input:  Bronze envelope:
              { "scope": {...}, "records": [
                  { "publication_number": "US-2025000826-A1",
                    "title_localized": [...], "cpc": [...], ... }, ... ] }
    Output: list of CanonicalRecord
            (entity_type = PATENT_GRANT if grant_date set, else PATENT_APPLICATION)

    entity_type rule verified against the actual data: grant_date is
    populated for exactly the kind_code/application_kind combinations
    that represent grants (US B2, EP B1, CN B, KR B1, CA C, and the
    EP-validation T-kind translations) and absent for every application
    kind (CN/US/EP/JP/KR/CA/AU A*, WO A1/A2/A3) — no per-country
    special-casing needed.
    """

    source_id:       str = "google_patents"
    source_category: str = "patents"
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

        raw_records = payload.get("records", [])
        records, skipped = [], 0
        for item in raw_records:
            try:
                record = self._map_record(item)
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

    def _map_record(self, item: Dict[str, Any]) -> Optional[CanonicalRecord]:
        pub_no = (item.get("publication_number") or "").strip()
        if not pub_no:
            return None

        country = (item.get("country_code") or "").strip().upper()
        kind_code = (item.get("kind_code") or "").strip()
        app_kind = (item.get("application_kind") or "").strip()

        # ── entity_type ─────────────────────────────────────────────────
        grant_date_raw = item.get("grant_date")
        is_grant = bool(grant_date_raw and grant_date_raw > 0)
        entity_type = EntityType.PATENT_GRANT if is_grant else EntityType.PATENT_APPLICATION

        # ── Title (required) / abstract (optional) ─────────────────────
        title, title_lang = _pick_localized_text(item.get("title_localized") or [])
        if not title:
            return None  # canonical schema requires a non-empty title
        abstract, _ = _pick_localized_text(
            item.get("abstract_localized") or [], preferred_lang=title_lang or "en",
        )

        published_at = _parse_yyyymmdd(item.get("publication_date"))

        # ── Actors ───────────────────────────────────────────────────────
        actors = []
        for a in item.get("assignee_harmonized") or []:
            name = (a.get("name") or "").strip()
            if name:
                actors.append(Actor(
                    name=name, role=ActorRole.ASSIGNEE,
                    country=(a.get("country_code") or "").strip() or None,
                ))
        for inv in item.get("inventor_harmonized") or []:
            name = (inv.get("name") or "").strip()
            if name:
                actors.append(Actor(
                    name=name, role=ActorRole.INVENTOR,
                    country=(inv.get("country_code") or "").strip() or None,
                ))

        # ── CPC ──────────────────────────────────────────────────────────
        cpc_codes = sorted({
            (c.get("code") or "").strip()
            for c in (item.get("cpc") or []) if c.get("code")
        })
        cpc_subclasses = sorted({
            code[:4].lower() for code in cpc_codes if len(code) >= 4
        })

        # ── Tags ─────────────────────────────────────────────────────────
        status_tag = "patent_grant" if is_grant else "patent_application"
        tags = list(dict.fromkeys(cpc_subclasses + [status_tag, country.lower()]))

        # ── Classifiers ──────────────────────────────────────────────────
        classifiers = {
            "country_code":       country,
            "kind_code":          kind_code,
            "application_kind":   app_kind,
            "family_id":          item.get("family_id"),
            "pct_number":         item.get("pct_number"),
            "application_number": item.get("application_number"),
            "filing_date":        item.get("filing_date"),
            "priority_date":      item.get("priority_date"),
            "grant_date":         grant_date_raw,
            "cpc_codes":          cpc_codes,
            "assignee_names":     [
                a.get("name") for a in (item.get("assignee_harmonized") or [])
                if a.get("name")
            ],
            "inventor_names":     [
                i.get("name") for i in (item.get("inventor_harmonized") or [])
                if i.get("name")
            ],
        }

        source_url = GOOGLE_PATENTS_SOURCE_URL_BASE + pub_no.replace("-", "")

        return CanonicalRecord(
            source_id    = self.source_id,
            source_type  = self.source_category,
            source_url   = source_url,
            external_id  = pub_no,
            published_at = published_at,
            entity_type  = entity_type,
            title        = title,
            summary      = abstract,
            region       = country,
            language     = (title_lang or "en")[:5],
            actors       = actors,
            tags         = tags,
            classifiers  = classifiers,
            lineage = Lineage(
                adapter_version = self.version,
                pipeline_run_id = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                llm_assisted    = False,
            ),
        )