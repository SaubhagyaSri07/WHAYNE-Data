import logging
from datetime import datetime, timezone
from typing import List, Optional

from lxml import etree

from schema import Actor, ActorRole, CanonicalRecord, EntityType, Lineage

logger = logging.getLogger(__name__)

PUBMED_URL_BASE = "https://pubmed.ncbi.nlm.nih.gov"

# PubMed uses ISO 639-3 codes; schema expects ISO 639-1 (2-char)
LANG_MAP = {
    "eng": "en", "fre": "fr", "ger": "de", "spa": "es",
    "chi": "zh", "jpn": "ja", "kor": "ko", "ita": "it",
    "por": "pt", "rus": "ru", "dut": "nl", "pol": "pl",
    "ara": "ar", "tur": "tr", "swe": "sv", "nor": "no",
    "dan": "da", "fin": "fi", "heb": "he", "hun": "hu",
    "ind": "id", "cze": "cs", "rum": "ro", "ukr": "uk",
}

MONTH_MAP = {
    "Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04",
    "May": "05", "Jun": "06", "Jul": "07", "Aug": "08",
    "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12",
}


class PubMedMap:
    """
    Maps raw PubMed XML (PubmedArticleSet) to CanonicalRecord objects.

    Input:  raw XML string from Bronze
    Output: list of CanonicalRecord
    """

    source_id:       str = "pubmed"
    source_category: str = "research"
    version:         str = "1.0.0"

    def normalise(self, raw_xml: str) -> List[CanonicalRecord]:
        if not raw_xml or not raw_xml.strip():
            logger.warning("[%s] Empty raw XML — nothing to normalise", self.source_id)
            return []

        try:
            root = etree.fromstring(raw_xml.encode("utf-8"))
        except etree.XMLSyntaxError as e:
            logger.error("[%s] XML parse error: %s", self.source_id, e)
            return []

        articles = root.findall(".//PubmedArticle")
        logger.info("[%s] Normalising %d articles", self.source_id, len(articles))

        records, skipped = [], 0
        for article in articles:
            try:
                record = self._map_article(article)
                if record:
                    records.append(record)
                else:
                    skipped += 1
            except Exception as e:
                logger.warning("[%s] Article mapping failed: %s", self.source_id, e)
                skipped += 1

        logger.info(
            "[%s] Normalised %d records (%d skipped)",
            self.source_id, len(records), skipped,
        )
        return records

    def _map_article(self, article) -> Optional[CanonicalRecord]:
        citation = article.find("MedlineCitation")
        if citation is None:
            return None

        art = citation.find("Article")
        if art is None:
            return None

        # ── PMID ──────────────────────────────────────────────────────────
        pmid_el = citation.find("PMID")
        pmid    = pmid_el.text.strip() if pmid_el is not None else None
        if not pmid:
            return None

        # ── Title ─────────────────────────────────────────────────────────
        title = self._element_text(art.find("ArticleTitle"))
        if not title:
            return None

        # ── Abstract ──────────────────────────────────────────────────────
        # Structured abstracts have labelled sections (BACKGROUND, METHODS…)
        # Unstructured abstracts have a single unlabelled AbstractText node.
        abstract_parts = []
        for ab in art.findall(".//AbstractText"):
            label = ab.get("Label")
            text  = self._element_text(ab)
            if text:
                abstract_parts.append(f"{label}: {text}" if label else text)
        summary = " ".join(abstract_parts) or None

        # ── Journal metadata ───────────────────────────────────────────────
        journal       = art.find("Journal")
        journal_title = None
        issn          = None
        volume        = None
        issue         = None
        pages         = None
        journal_date  = None

        if journal is not None:
            issn_el = journal.find("ISSN")
            if issn_el is not None and issn_el.text:
                issn = issn_el.text.strip()

            title_el = journal.find("Title")
            if title_el is not None and title_el.text:
                journal_title = title_el.text.strip()

            ji = journal.find("JournalIssue")
            if ji is not None:
                vol_el = ji.find("Volume")
                if vol_el is not None and vol_el.text:
                    volume = vol_el.text.strip()
                iss_el = ji.find("Issue")
                if iss_el is not None and iss_el.text:
                    issue = iss_el.text.strip()
                journal_date = self._parse_pubdate(ji.find("PubDate"))

        # Pagination — stored as "1234-8" or "1234-1240"
        pag_el = art.find("Pagination/MedlinePgn")
        if pag_el is not None and pag_el.text:
            pages = pag_el.text.strip()

        # Prefer ArticleDate (e-pub, always full YYYY-MM-DD) over journal
        # issue PubDate which is often month-only or year-only.
        article_date_el = art.find("ArticleDate[@DateType='Electronic']")
        published_at    = self._parse_articledate(article_date_el) or journal_date

        # ── Authors → actors ───────────────────────────────────────────────
        actors = []
        author_list = art.find("AuthorList")
        if author_list is not None:
            for author in author_list.findall("Author"):
                name = self._author_name(author)
                if name:
                    affil_el = author.find(".//AffiliationInfo/Affiliation")
                    affil    = affil_el.text.strip() if affil_el is not None else None
                    actors.append(Actor(
                        name    = name,
                        role    = ActorRole.AUTHOR,
                        address = affil,
                    ))

        # ── Tags: MeSH terms + keywords ────────────────────────────────────
        # CanonicalRecord validator handles lowercase + dedup — no need to
        # replicate that logic here. Just collect all terms.
        tags = []
        for mesh in citation.findall(".//MeshHeading/DescriptorName"):
            if mesh.text:
                tags.append(mesh.text.strip())
        for kw in citation.findall(".//KeywordList/Keyword"):
            if kw.text:
                tags.append(kw.text.strip())

        # ── Publication types ──────────────────────────────────────────────
        # e.g. "Journal Article", "Randomized Controlled Trial", "Review"
        pub_types = []
        for pt in art.findall(".//PublicationTypeList/PublicationType"):
            if pt.text:
                pub_types.append(pt.text.strip())

        # ── DOI ────────────────────────────────────────────────────────────
        # Scoped extraction — avoids ReferenceList DOI bleed-through where
        # .//ArticleId would also iterate reference entries.
        doi = self._extract_article_doi(article, art)

        # ── PMC ID ─────────────────────────────────────────────────────────
        pmc = None
        pubmed_data = article.find("PubmedData")
        if pubmed_data is not None:
            pmc_el = pubmed_data.find("ArticleIdList/ArticleId[@IdType='pmc']")
            if pmc_el is not None and pmc_el.text:
                pmc = pmc_el.text.strip()

        # ── Language ───────────────────────────────────────────────────────
        # PubMed returns ISO 639-3 (3-char); map to ISO 639-1 (2-char).
        lang_el  = art.find("Language")
        raw_lang = lang_el.text.strip().lower() if lang_el is not None else "eng"
        language = LANG_MAP.get(raw_lang, raw_lang[:2])  # fallback: first 2 chars

        # ── Classifiers ────────────────────────────────────────────────────
        # Use None for absent values so downstream queries can distinguish
        # "not present" from "explicitly empty".
        classifiers = {k: v for k, v in {
            "pmid":      pmid,
            "doi":       doi        or None,
            "pmc":       pmc,
            "journal":   journal_title,
            "issn":      issn,
            "volume":    volume,
            "issue":     issue,
            "pages":     pages,
            "pub_types": pub_types  or None,
        }.items() if v is not None}

        return CanonicalRecord(
            source_id    = self.source_id,
            source_type  = self.source_category,
            source_url   = f"{PUBMED_URL_BASE}/{pmid}/",
            external_id  = f"PMID-{pmid}",
            published_at = published_at,
            entity_type  = EntityType.RESEARCH_PAPER,
            title        = title,
            summary      = summary,
            region       = None,
            language     = language,
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
    def _extract_article_doi(article, art) -> str:
        """
        Extract the article's own DOI.

        Primary:  PubmedData/ArticleIdList/ArticleId[@IdType='doi']
                  Scoped to PubmedData — avoids ReferenceList entries which
                  also carry <ArticleId IdType="doi"> nodes.

        Fallback: Article/ELocationID[@EIdType='doi']
                  Present when the DOI is declared in the citation block
                  rather than PubmedData (older records).
        """
        pubmed_data = article.find("PubmedData")
        if pubmed_data is not None:
            el = pubmed_data.find("ArticleIdList/ArticleId[@IdType='doi']")
            if el is not None and el.text:
                return el.text.strip()

        el = art.find("ELocationID[@EIdType='doi']")
        if el is not None and el.text:
            return el.text.strip()

        return ""

    @staticmethod
    def _element_text(el) -> str:
        """Return full text of an element, collapsing mixed-content sub-tags."""
        if el is None:
            return ""
        return "".join(el.itertext()).strip()

    @staticmethod
    def _author_name(author) -> str:
        last = author.findtext("LastName",       "").strip()
        fore = author.findtext("ForeName",       "").strip()
        coll = author.findtext("CollectiveName", "").strip()
        if coll:
            return coll
        if last and fore:
            return f"{fore} {last}"
        return last or fore

    @staticmethod
    def _parse_pubdate(pubdate_el) -> Optional[datetime]:
        """
        Parse a PubDate element. Handles four formats:
          - Year + Month + Day  (complete)
          - Year + Month        (day defaults to 01)
          - Year only           (month and day default to 01)
          - MedlineDate string  e.g. "2024 Jan-Feb" (year extracted from prefix)
        """
        if pubdate_el is None:
            return None

        year  = pubdate_el.findtext("Year",  "").strip()
        month = pubdate_el.findtext("Month", "").strip()
        day   = pubdate_el.findtext("Day",   "").strip()

        if not year:
            med = pubdate_el.findtext("MedlineDate", "").strip()
            if med:
                year = med[:4]

        if not year:
            return None

        month = MONTH_MAP.get(month, month).zfill(2) if month else "01"
        day   = day.zfill(2) if day.isdigit() else "01"

        try:
            return datetime.strptime(f"{year}-{month}-{day}", "%Y-%m-%d")
        except ValueError:
            return None

    @staticmethod
    def _parse_articledate(el) -> Optional[datetime]:
        """
        Parse an ArticleDate element (electronic pub date).
        These always carry full Year/Month/Day — more precise than PubDate.
        """
        if el is None:
            return None
        year  = el.findtext("Year",  "").strip()
        month = el.findtext("Month", "").strip().zfill(2)
        day   = el.findtext("Day",   "").strip().zfill(2)
        if not (year and month and day):
            return None
        try:
            return datetime.strptime(f"{year}-{month}-{day}", "%Y-%m-%d")
        except ValueError:
            return None