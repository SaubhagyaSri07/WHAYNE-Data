import json
import logging
from datetime import datetime
from typing import List, Optional
from urllib.parse import urlparse

import feedparser

from schema import Actor, ActorRole, CanonicalRecord, EntityType, Lineage

logger = logging.getLogger(__name__)


class RSSMap:
    """
    Maps raw RSS/Atom feed JSON (from Bronze) to CanonicalRecord objects.

    Input:  Bronze JSON envelope:
              { "feeds": [ { "url": "...", "raw_xml": "..." }, ... ] }
    Output: list of CanonicalRecord (entity_type = NEWS_ARTICLE)

    feedparser abstracts RSS 0.9x / RSS 1.0 / RSS 2.0 / Atom 0.3 / Atom 1.0
    differences — the normaliser doesn't need to care which format each feed uses.
    """

    source_id:       str = "rss"
    source_category: str = "news"
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

        records = []
        for feed_data in payload.get("feeds", []):
            url     = feed_data.get("url", "")
            raw_xml = feed_data.get("raw_xml")

            if not raw_xml:
                logger.warning("[%s] Skipping %s — no raw XML", self.source_id, url)
                continue

            feed_records = self._process_feed(url, raw_xml)
            records.extend(feed_records)

        logger.info("[%s] Normalised %d records across all feeds", self.source_id, len(records))
        return records

    def _process_feed(self, feed_url: str, raw_xml: str) -> List[CanonicalRecord]:
        try:
            parsed = feedparser.parse(raw_xml)
        except Exception as e:
            logger.warning("[%s] feedparser failed for %s: %s", self.source_id, feed_url, e)
            return []

        if parsed.bozo and not parsed.entries:
            logger.warning(
                "[%s] Malformed feed %s: %s",
                self.source_id, feed_url, parsed.bozo_exception,
            )
            return []

        feed_title = parsed.feed.get("title", "").strip() or self._domain(feed_url)
        feed_lang  = parsed.feed.get("language", "en")[:5] or "en"

        records, skipped = [], 0
        for entry in parsed.entries:
            try:
                record = self._map_entry(entry, feed_url, feed_title, feed_lang)
                if record:
                    records.append(record)
                else:
                    skipped += 1
            except Exception as e:
                logger.warning(
                    "[%s] Entry mapping failed (%s): %s",
                    self.source_id, feed_url, e,
                )
                skipped += 1

        logger.info(
            "[%s] %-50s → %d records (%d skipped)",
            self.source_id, feed_title[:50], len(records), skipped,
        )
        return records

    def _map_entry(
        self,
        entry,
        feed_url:   str,
        feed_title: str,
        feed_lang:  str,
    ) -> Optional[CanonicalRecord]:
        # ── Title (required) ──────────────────────────────────────────────
        title = (getattr(entry, "title", "") or "").strip()
        if not title:
            return None

        # ── Source URL ────────────────────────────────────────────────────
        source_url = (getattr(entry, "link", "") or "").strip() or feed_url

        # ── External ID ───────────────────────────────────────────────────
        # entry.id is the guid/permalink; fall back to entry link
        raw_id     = (getattr(entry, "id", "") or "").strip()
        external_id = raw_id or source_url or None

        # ── Published at ──────────────────────────────────────────────────
        published_at = self._parse_feedparser_date(
            getattr(entry, "published_parsed", None)
            or getattr(entry, "updated_parsed", None)
        )

        # ── Summary ───────────────────────────────────────────────────────
        summary = (
            getattr(entry, "summary", "")
            or getattr(entry, "description", "")
            or ""
        ).strip() or None

        # ── Actors ────────────────────────────────────────────────────────
        actors = []

        # Named author (if present in entry)
        author = (getattr(entry, "author", "") or "").strip()
        if author:
            actors.append(Actor(name=author, role=ActorRole.AUTHOR))

        # Feed organisation as publisher
        actors.append(Actor(name=feed_title, role=ActorRole.PUBLISHER))

        # ── Tags: from entry categories ───────────────────────────────────
        tags = []
        for tag in getattr(entry, "tags", []):
            term = (tag.get("term") or "").strip()
            if term:
                tags.append(term)

        # ── Classifiers ───────────────────────────────────────────────────
        classifiers = {
            "feed_title": feed_title,
            "feed_url":   feed_url,
        }

        return CanonicalRecord(
            source_id    = self.source_id,
            source_type  = self.source_category,
            source_url   = source_url,
            external_id  = external_id,
            published_at = published_at,
            entity_type  = EntityType.NEWS_ARTICLE,
            title        = title,
            summary      = summary,
            region       = None,
            language     = feed_lang,
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
    def _parse_feedparser_date(t) -> Optional[datetime]:
        """Convert feedparser's time.struct_time to datetime."""
        if t is None:
            return None
        try:
            return datetime(*t[:6])
        except Exception:
            return None

    @staticmethod
    def _domain(url: str) -> str:
        """Extract domain from URL for use as fallback feed title."""
        try:
            return urlparse(url).netloc
        except Exception:
            return url