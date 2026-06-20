import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from schema import Actor, ActorRole, CanonicalRecord, EntityType, Lineage

logger = logging.getLogger(__name__)

USPTO_PATENT_URL = "https://patents.google.com/patent/US{number}"


class USPTOMap:
    """
    Maps raw PatentsView JSON to CanonicalRecord objects.

    Input:  Bronze JSON from PatentsView:
              { "patents": [...], "count": N, "total_patent_count": N }
    Output: list of CanonicalRecord (entity_type = PATENT_GRANT)

    PatentsView response nests inventors, assignees, and CPC codes
    as arrays within each patent record.
    """

    source_id:       str = "uspto"
    source_category: str = "patents"
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

        patents = payload.get("patents") or []
        total   = payload.get("total_patent_count", 0)
        logger.info(
            "[%s] Normalising %d patents (total available: %d)",
            self.source_id, len(patents), total,
        )

        records, skipped = [], 0
        for patent in patents:
            try:
                record = self._map_patent(patent)
                if record:
                    records.append(record)
                else:
                    skipped += 1
            except Exception as e:
                num = patent.get("patent_number", "unknown")
                logger.warning(
                    "[%s] Patent mapping failed (US%s): %s",
                    self.source_id, num, e,
                )
                skipped += 1

        logger.info(
            "[%s] Normalised %d records (%d skipped)",
            self.source_id, len(records), skipped,
        )
        return records

    def _map_patent(self, p: Dict[str, Any]) -> Optional[CanonicalRecord]:
        # ── Patent number (required) ──────────────────────────────────────
        patent_number = (p.get("patent_number") or "").strip()
        if not patent_number:
            return None

        # ── Title (required) ──────────────────────────────────────────────
        title = (p.get("patent_title") or "").strip()
        if not title:
            return None

        # ── Abstract ──────────────────────────────────────────────────────
        summary = (p.get("patent_abstract") or "").strip() or None

        # ── Grant date ────────────────────────────────────────────────────
        published_at = self._parse_date(p.get("patent_date"))

        # ── Actors: assignees (primary) + inventors (secondary) ───────────
        actors = []

        assignees = p.get("assignees") or []
        for a in assignees:
            org = (a.get("assignee_organization") or "").strip()
            if org:
                actors.append(Actor(
                    name    = org,
                    role    = ActorRole.ASSIGNEE,
                    country = (a.get("assignee_country") or "").strip() or None,
                ))

        inventors = p.get("inventors") or []
        for inv in inventors:
            first = (inv.get("inventor_first_name") or "").strip()
            last  = (inv.get("inventor_last_name")  or "").strip()
            name  = f"{first} {last}".strip()
            if name:
                actors.append(Actor(
                    name    = name,
                    role    = ActorRole.INVENTOR,
                    country = (inv.get("inventor_country") or "").strip() or None,
                ))

        # ── Tags: CPC subgroup codes (e.g. "A61K31/00", "A61B10") ─────────
        # validator lowercases + deduplicates
        cpcs = p.get("cpcs") or []
        tags = []
        seen = set()
        for cpc in cpcs:
            # Use subgroup for specificity, fall back to group
            code = (
                cpc.get("cpc_subgroup_id")
                or cpc.get("cpc_group_id")
                or ""
            ).strip()
            if code and code not in seen:
                seen.add(code)
                tags.append(code)

        # ── Classifiers ───────────────────────────────────────────────────
        cpc_groups = list({
            c.get("cpc_group_id", "") for c in cpcs
            if c.get("cpc_group_id")
        })

        classifiers = {
            "patent_number": patent_number,
            "patent_type":   (p.get("patent_type") or "").strip(),
            "cpc_groups":    sorted(cpc_groups)[:20],  # top 20 CPC groups
        }

        return CanonicalRecord(
            source_id    = self.source_id,
            source_type  = self.source_category,
            source_url   = USPTO_PATENT_URL.format(number=patent_number),
            external_id  = f"US{patent_number}",
            published_at = published_at,
            entity_type  = EntityType.PATENT_GRANT,
            title        = title,
            summary      = summary,
            region       = "US",
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

    @staticmethod
    def _parse_date(date_str: str) -> Optional[datetime]:
        """Parse PatentsView date strings (YYYY-MM-DD)."""
        if not date_str:
            return None
        try:
            return datetime.strptime(date_str.strip(), "%Y-%m-%d")
        except ValueError:
            return None