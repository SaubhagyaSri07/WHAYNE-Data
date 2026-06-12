import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from schema import Actor, ActorRole, CanonicalRecord, EntityType, Lineage

logger = logging.getLogger(__name__)

EPMC_ARTICLE_URL = "https://europepmc.org/article/MED/{pmid}"
DOI_URL          = "https://doi.org/{doi}"

_HTML_TAG = re.compile(r"<[^>]+>")


class CochraneMap:
    """
    Maps raw Europe PMC JSON to CanonicalRecord objects for
    Cochrane Database of Systematic Reviews entries.

    Input:  raw Europe PMC JSON string from Bronze
    Output: list of CanonicalRecord (entity_type = RESEARCH_PAPER)

    Europe PMC response envelope:
      {
        "hitCount": N,
        "nextCursorMark": "...",
        "resultList": {
          "result": [ { ...article fields... }, ... ]
        }
      }
    """

    source_id:       str = "cochrane"
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

        results = payload.get("resultList", {}).get("result", [])
        hit_count = payload.get("hitCount", 0)
        logger.info(
            "[%s] Normalising %d results (total available: %d)",
            self.source_id, len(results), hit_count,
        )

        records, skipped = [], 0
        for item in results:
            try:
                record = self._map_result(item)
                if record:
                    records.append(record)
                else:
                    skipped += 1
            except Exception as e:
                pmid = item.get("pmid", "unknown")
                logger.warning(
                    "[%s] Result mapping failed (PMID %s): %s",
                    self.source_id, pmid, e,
                )
                skipped += 1

        logger.info(
            "[%s] Normalised %d records (%d skipped)",
            self.source_id, len(records), skipped,
        )
        return records

    def _map_result(self, r: Dict[str, Any]) -> Optional[CanonicalRecord]:
        # ── Title (required) ──────────────────────────────────────────────
        title = (r.get("title") or "").strip()
        title = self._strip_html(title)
        if not title:
            return None

        # ── Identifiers ───────────────────────────────────────────────────
        pmid  = (r.get("pmid")  or "").strip()
        doi   = (r.get("doi")   or "").strip()
        pmcid = (r.get("pmcid") or "").strip()

        # external_id: prefer DOI (globally unique and stable)
        external_id = f"DOI-{doi}" if doi else (f"PMID-{pmid}" if pmid else None)

        # source_url: prefer DOI resolver, fall back to Europe PMC page
        source_url = (
            DOI_URL.format(doi=doi)          if doi  else
            EPMC_ARTICLE_URL.format(pmid=pmid) if pmid else
            "https://europepmc.org"
        )

        # ── Published at ──────────────────────────────────────────────────
        published_at = self._parse_date(r.get("firstPublicationDate") or r.get("pubYear"))

        # ── Abstract ──────────────────────────────────────────────────────
        summary = self._strip_html((r.get("abstractText") or "").strip()) or None

        # ── Authors ───────────────────────────────────────────────────────
        actors = []
        author_list = r.get("authorList", {}).get("author", [])
        if isinstance(author_list, list):
            for author in author_list:
                name = (
                    author.get("fullName")
                    or f"{author.get('firstName', '')} {author.get('lastName', '')}".strip()
                ).strip()
                if name:
                    affil = (author.get("affiliation") or "").strip() or None
                    actors.append(Actor(
                        name    = name,
                        role    = ActorRole.AUTHOR,
                        address = affil,
                    ))

        # ── Tags: keywords ────────────────────────────────────────────────
        # validator lowercases + deduplicates
        kw_block = r.get("keywordList", {})
        if isinstance(kw_block, dict):
            raw_kws = kw_block.get("keyword", [])
            tags = [k.strip() for k in raw_kws if isinstance(k, str) and k.strip()]
        else:
            tags = []

        # ── Classifiers ───────────────────────────────────────────────────
        classifiers = {
            "pmid":         pmid,
            "doi":          doi,
            "pmcid":        pmcid,
            "pub_year":     str(r.get("pubYear") or ""),
            "journal":      (r.get("journalTitle") or "").strip(),
            "source":       (r.get("source") or "").strip(),
            "issue":        (r.get("issue") or "").strip(),
            "author_string": (r.get("authorString") or "").strip()[:500],
        }

        return CanonicalRecord(
            source_id    = self.source_id,
            source_type  = self.source_category,
            source_url   = source_url,
            external_id  = external_id,
            published_at = published_at,
            entity_type  = EntityType.RESEARCH_PAPER,
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
    def _strip_html(text: str) -> str:
        """Remove HTML/XML tags from abstractText which can contain markup."""
        return _HTML_TAG.sub("", text).strip()

    @staticmethod
    def _parse_date(date_str: str) -> Optional[datetime]:
        """
        Parse Europe PMC date strings.
        Handles: "YYYY-MM-DD", "YYYY-MM", "YYYY"
        """
        if not date_str:
            return None
        for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
            try:
                return datetime.strptime(str(date_str).strip(), fmt)
            except ValueError:
                continue
        return None