import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from schema import Actor, ActorRole, CanonicalRecord, EntityType, Lineage

logger = logging.getLogger(__name__)

CT_URL_BASE = "https://clinicaltrials.gov/study"


class ClinicalTrialsMap:
    """
    Maps raw ClinicalTrials.gov v2 API JSON to CanonicalRecord objects.

    Input:  raw JSON string from Bronze (full API response envelope)
    Output: list of CanonicalRecord
    """

    source_id:       str = "clinicaltrials"
    source_category: str = "research"
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

        studies = payload.get("studies", [])
        logger.info("[%s] Normalising %d studies", self.source_id, len(studies))

        records, skipped = [], 0
        for study in studies:
            try:
                record = self._map_study(study)
                if record:
                    records.append(record)
                else:
                    skipped += 1
            except Exception as e:
                nct = self._s(study, "protocolSection", "identificationModule", "nctId")
                logger.warning(
                    "[%s] Study mapping failed (%s): %s",
                    self.source_id, nct or "unknown", e,
                )
                skipped += 1

        logger.info(
            "[%s] Normalised %d records (%d skipped)",
            self.source_id, len(records), skipped,
        )
        return records

    def _map_study(self, study: Dict[str, Any]) -> Optional[CanonicalRecord]:
        proto = study.get("protocolSection", {})
        if not proto:
            return None

        id_mod       = proto.get("identificationModule",       {})
        status_mod   = proto.get("statusModule",               {})
        desc_mod     = proto.get("descriptionModule",          {})
        cond_mod     = proto.get("conditionsModule",           {})
        design_mod   = proto.get("designModule",               {})
        arms_mod     = proto.get("armsInterventionsModule",    {})
        sponsor_mod  = proto.get("sponsorCollaboratorsModule", {})
        contacts_mod = proto.get("contactsLocationsModule",    {})

        # ── NCT ID ────────────────────────────────────────────────────────
        nct_id = id_mod.get("nctId", "").strip()
        if not nct_id:
            return None

        # ── Title ─────────────────────────────────────────────────────────
        brief_title    = id_mod.get("briefTitle",    "").strip()
        official_title = id_mod.get("officialTitle", "").strip()
        title = brief_title or official_title
        if not title:
            return None

        # ── Summary ───────────────────────────────────────────────────────
        summary = desc_mod.get("briefSummary", "").strip() or None

        # ── Published at (date first posted publicly) ──────────────────────
        published_at = self._parse_ct_date(
            self._s(status_mod, "studyFirstPostDateStruct", "date")
        )

        # ── Actors ────────────────────────────────────────────────────────
        actors = []

        lead_name = self._s(sponsor_mod, "leadSponsor", "name").strip()
        if lead_name:
            actors.append(Actor(name=lead_name, role=ActorRole.SPONSOR))

        for collab in sponsor_mod.get("collaborators", []):
            name = collab.get("name", "").strip()
            if name:
                actors.append(Actor(name=name, role=ActorRole.SPONSOR))

        for official in contacts_mod.get("overallOfficials", []):
            name  = official.get("name",        "").strip()
            affil = official.get("affiliation", "").strip() or None
            if name:
                actors.append(Actor(
                    name    = name,
                    role    = ActorRole.INVESTIGATOR,
                    address = affil,
                ))

        # ── Tags: conditions + keywords + interventions ────────────────────
        # ClinicalTrials keywords are sometimes comma-separated phrases
        # stored as a single list item — split them so each becomes its
        # own tag. Conditions and intervention names are not split because
        # they are distinct, well-formed entries from the API.
        # CanonicalRecord validator handles lowercase + dedup downstream.
        tags = []

        for cond in cond_mod.get("conditions", []):
            if cond and cond.strip():
                tags.append(cond.strip())

        for kw in cond_mod.get("keywords", []):
            if kw and kw.strip():
                # Split comma-separated keyword phrases into individual tags
                for part in kw.split(","):
                    part = part.strip()
                    if part:
                        tags.append(part)

        for interv in arms_mod.get("interventions", []):
            name = interv.get("name", "").strip()
            if name:
                tags.append(name)

        # ── Phase ─────────────────────────────────────────────────────────
        # Joined string e.g. "PHASE2/PHASE3"; None when not applicable
        phases = design_mod.get("phases", [])
        phase  = "/".join(phases) if phases else None

        # ── Enrollment ────────────────────────────────────────────────────
        enrollment_info  = design_mod.get("enrollmentInfo") or {}
        enrollment_count = enrollment_info.get("count")  # int or None

        # ── Classifiers ───────────────────────────────────────────────────
        # Dates kept as raw strings (YYYY-MM-DD or YYYY-MM) from the API.
        # Use None for absent values — distinguishes "not present" from "".
        classifiers = {k: v for k, v in {
            "nct_id":                  nct_id,
            "phase":                   phase,
            "overall_status":          status_mod.get("overallStatus") or None,
            "study_type":              design_mod.get("studyType")     or None,
            "enrollment_count":        enrollment_count,
            "official_title":          official_title                   or None,
            "start_date":              self._s(status_mod, "startDateStruct",             "date") or None,
            "primary_completion_date": self._s(status_mod, "primaryCompletionDateStruct", "date") or None,
            "completion_date":         self._s(status_mod, "completionDateStruct",         "date") or None,
        }.items() if v is not None}

        return CanonicalRecord(
            source_id    = self.source_id,
            source_type  = self.source_category,
            source_url   = f"{CT_URL_BASE}/{nct_id}",
            external_id  = nct_id,
            published_at = published_at,
            entity_type  = EntityType.CLINICAL_TRIAL,
            title        = title,
            summary      = summary,
            region       = None,
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

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _s(d: Any, *keys: str) -> str:
        """
        Safe nested dict accessor for string leaf values.
        Returns "" if any key is missing or the value is not a string.
        """
        for k in keys:
            if not isinstance(d, dict):
                return ""
            d = d.get(k)
            if d is None:
                return ""
        return d if isinstance(d, str) else ""

    @staticmethod
    def _parse_ct_date(date_str: str) -> Optional[datetime]:
        """
        Parse a ClinicalTrials date string.
        Handles: "YYYY-MM-DD", "YYYY-MM", "YYYY"
        """
        if not date_str:
            return None
        for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
            try:
                return datetime.strptime(date_str.strip(), fmt)
            except ValueError:
                continue
        return None