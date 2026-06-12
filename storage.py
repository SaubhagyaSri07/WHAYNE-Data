import json
import logging
import os
from datetime import datetime
from typing import List, Optional

from schema import CanonicalRecord

logger = logging.getLogger(__name__)

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
BRONZE_DIR  = os.path.join(BASE_DIR, "bronze")
SILVER_DIR  = os.path.join(BASE_DIR, "silver")


# ─────────────────────────────────────────────────────────────────────────────
# BRONZE  — raw, untouched, exactly as received from source
# ─────────────────────────────────────────────────────────────────────────────

def write_bronze(
    category:   str,
    source_id:  str,
    raw:        str,
    extension:  str = "json",
) -> str:
    """
    Write raw source response to Bronze.

    Args:
        category:  e.g. "research", "regulatory", "patents"
        source_id: e.g. "pubmed", "fda_510k"
        raw:       raw response string exactly as received
        extension: file extension — json, xml, html

    Returns:
        Full path of the written file.
    """
    date_str  = datetime.utcnow().strftime("%Y-%m-%d")
    time_str  = datetime.utcnow().strftime("%H%M%S")
    directory = os.path.join(BRONZE_DIR, category, source_id, date_str)
    os.makedirs(directory, exist_ok=True)

    filepath = os.path.join(directory, f"raw_{time_str}.{extension}")
    with open(filepath, "w", encoding="utf-8", newline="") as f:
        f.write(raw)

    logger.info(f"[bronze] {source_id} → {filepath}")
    return filepath


def read_bronze(filepath: str) -> str:
    """Read raw content from a Bronze file."""
    with open(filepath, "r", encoding="utf-8", newline="") as f:
        return f.read()


def list_bronze(
    category:  str,
    source_id: str,
    date:      Optional[str] = None,
) -> List[str]:
    """
    List all Bronze files for a source.
    If date is provided (YYYY-MM-DD), list only that day's files.
    """
    base = os.path.join(BRONZE_DIR, category, source_id)
    if not os.path.exists(base):
        return []

    files = []
    if date:
        day_dir = os.path.join(base, date)
        if os.path.exists(day_dir):
            files = [
                os.path.join(day_dir, f)
                for f in sorted(os.listdir(day_dir))
            ]
    else:
        for day in sorted(os.listdir(base)):
            day_dir = os.path.join(base, day)
            if os.path.isdir(day_dir):
                for f in sorted(os.listdir(day_dir)):
                    files.append(os.path.join(day_dir, f))

    return files


# ─────────────────────────────────────────────────────────────────────────────
# SILVER  — canonical, normalised, schema-validated records
# ─────────────────────────────────────────────────────────────────────────────

def write_silver(
    category:  str,
    source_id: str,
    records:   List[CanonicalRecord],
) -> str:
    """
    Write normalised canonical records to Silver as JSON.

    Args:
        category:  e.g. "research", "regulatory"
        source_id: e.g. "pubmed", "fda_510k"
        records:   list of validated CanonicalRecord objects

    Returns:
        Full path of the written file.
    """
    if not records:
        logger.warning(f"[silver] {source_id} — no records to write")
        return ""

    date_str  = datetime.utcnow().strftime("%Y-%m-%d")
    time_str  = datetime.utcnow().strftime("%H%M%S")
    directory = os.path.join(SILVER_DIR, category, source_id, date_str)
    os.makedirs(directory, exist_ok=True)

    filepath = os.path.join(directory, f"normalised_{time_str}.json")
    with open(filepath, "w", encoding="utf-8", newline="") as f:
        json.dump(
            [r.model_dump(mode="json") for r in records],
            f,
            indent=2,
            default=str,
        )

    logger.info(
        f"[silver] {source_id} → {len(records)} records → {filepath}"
    )
    return filepath


def read_silver(filepath: str) -> List[dict]:
    """Read normalised records from a Silver file."""
    with open(filepath, "r", encoding="utf-8", newline="") as f:
        return json.load(f)


def list_silver(
    category:  str,
    source_id: str,
    date:      Optional[str] = None,
) -> List[str]:
    """List all Silver files for a source."""
    base = os.path.join(SILVER_DIR, category, source_id)
    if not os.path.exists(base):
        return []

    files = []
    if date:
        day_dir = os.path.join(base, date)
        if os.path.exists(day_dir):
            files = [
                os.path.join(day_dir, f)
                for f in sorted(os.listdir(day_dir))
            ]
    else:
        for day in sorted(os.listdir(base)):
            day_dir = os.path.join(base, day)
            if os.path.isdir(day_dir):
                for f in sorted(os.listdir(day_dir)):
                    files.append(os.path.join(day_dir, f))

    return files


# ─────────────────────────────────────────────────────────────────────────────
# DISPLAY  — print a sample of records to terminal
# ─────────────────────────────────────────────────────────────────────────────

def print_sample(records: List[CanonicalRecord], n: int = 2) -> None:
    for i, r in enumerate(records[:n]):
        actor = r.primary_actor()
        print(f"\n  ── Record {i + 1} {'─' * 35}")
        print(f"    source       : {r.source_id}")
        print(f"    entity_type  : {r.entity_type}")
        print(f"    external_id  : {r.external_id}")
        print(f"    title        : {r.title[:80]}")
        print(f"    published_at : {r.published_at}")
        print(f"    region       : {r.region}")
        print(f"    primary actor: {actor.name[:60] if actor else 'none'}")
        print(f"    tags         : {r.tags[:5]}")
        print(f"    classifiers  : {list(r.classifiers.keys())}")
        print(f"    content_hash : {r.content_hash[:20]}...")
        print(f"    llm_assisted : {r.lineage.llm_assisted if r.lineage else 'N/A'}")