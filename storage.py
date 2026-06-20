import json
import logging
import os
from datetime import datetime, timezone
from typing import List, Optional

import boto3
from botocore.config import Config
from dotenv import load_dotenv

from schema import CanonicalRecord

# Load variables from .env in the project root (no-op if the file
# doesn't exist, e.g. in a deployed environment using real env vars).
load_dotenv()

logger = logging.getLogger(__name__)

# ── Silver stays local (Bronze only moved to R2 per manager decision) ─────────
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
SILVER_DIR = os.path.join(BASE_DIR, "silver")

# ── R2 / Bronze configuration — all values read from environment ──────────────
# Set before running the pipeline (PowerShell):
#   $env:R2_ACCOUNT_ID        = "your_account_id"
#   $env:R2_ACCESS_KEY_ID     = "your_r2_access_key_id"
#   $env:R2_SECRET_ACCESS_KEY = "your_r2_secret_access_key"
#   $env:R2_BRONZE_BUCKET     = "whayne-bronze"   (or whatever bucket name)
#
# Get these from: Cloudflare Dashboard → R2 → Overview (Account ID top-right)
#   R2 → Manage R2 API Tokens → Create Token (Object Read & Write on bucket)
R2_ACCOUNT_ID        = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID     = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BRONZE_BUCKET     = os.environ.get("R2_BRONZE_BUCKET", "whayne-bronze")

# Object key convention mirrors the old local path structure exactly:
#   bronze/<category>/<source_id>/<YYYY-MM-DD>/raw_<HHMMSS>.<ext>
# This keeps directory-style listing, date partitioning, and replay semantics
# identical to what the local implementation provided.
BRONZE_KEY_PREFIX = "bronze"


def _r2_client():
    """
    Build a boto3 S3 client pointed at Cloudflare R2.
    R2 is S3-compatible — the only difference is the custom endpoint_url.
    Called per-operation rather than at module load so missing env vars
    produce a clear error at the point of use, not at import time.
    """
    missing = [
        name for name, val in [
            ("R2_ACCOUNT_ID",        R2_ACCOUNT_ID),
            ("R2_ACCESS_KEY_ID",     R2_ACCESS_KEY_ID),
            ("R2_SECRET_ACCESS_KEY", R2_SECRET_ACCESS_KEY),
        ] if not val
    ]
    if missing:
        raise EnvironmentError(
            f"[bronze/R2] Missing required environment variables: {missing}. "
            f"Set them before running the pipeline."
        )

    return boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        config=Config(signature_version="s3v4"),
        region_name="auto",   # R2 requires "auto" — no real AWS region
    )


# ─────────────────────────────────────────────────────────────────────────────
# BRONZE  — raw, untouched, exactly as received from source → Cloudflare R2
# ─────────────────────────────────────────────────────────────────────────────

def write_bronze(
    category:  str,
    source_id: str,
    raw:       str,
    extension: str = "json",
) -> str:
    """
    Upload raw source response to Bronze (Cloudflare R2).

    Args:
        category:  e.g. "research", "regulatory", "patents"
        source_id: e.g. "pubmed", "fda_510k"
        raw:       raw response string exactly as received
        extension: file extension — json, xml, html

    Returns:
        R2 object key (mirrors the old local path structure).
    """
    now      = datetime.now(timezone.utc).replace(tzinfo=None)
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H%M%S")

    key = f"{BRONZE_KEY_PREFIX}/{category}/{source_id}/{date_str}/raw_{time_str}.{extension}"

    content_type_map = {
        "json": "application/json",
        "xml":  "application/xml",
        "html": "text/html",
    }
    content_type = content_type_map.get(extension, "application/octet-stream")

    _r2_client().put_object(
        Bucket=R2_BRONZE_BUCKET,
        Key=key,
        Body=raw.encode("utf-8"),
        ContentType=content_type,
    )

    logger.info("[bronze/R2] %s → s3://%s/%s", source_id, R2_BRONZE_BUCKET, key)
    return key


def read_bronze(key: str) -> str:
    """
    Download raw content from a Bronze R2 object.

    Args:
        key: R2 object key as returned by write_bronze()
    """
    response = _r2_client().get_object(Bucket=R2_BRONZE_BUCKET, Key=key)
    return response["Body"].read().decode("utf-8")


def list_bronze(
    category:  str,
    source_id: str,
    date:      Optional[str] = None,
) -> List[str]:
    """
    List Bronze object keys for a source in R2.
    If date is provided (YYYY-MM-DD), list only that day's objects.

    Returns:
        List of R2 object keys.
    """
    prefix = f"{BRONZE_KEY_PREFIX}/{category}/{source_id}/"
    if date:
        prefix += f"{date}/"

    client   = _r2_client()
    paginator = client.get_paginator("list_objects_v2")
    keys     = []

    for page in paginator.paginate(Bucket=R2_BRONZE_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            keys.append(obj["Key"])

    return sorted(keys)


# ─────────────────────────────────────────────────────────────────────────────
# SILVER  — canonical, normalised, schema-validated records → local disk
# ─────────────────────────────────────────────────────────────────────────────

def write_silver(
    category:  str,
    source_id: str,
    records:   List[CanonicalRecord],
) -> str:
    """
    Write normalised canonical records to Silver as JSON (local disk).

    Args:
        category:  e.g. "research", "regulatory"
        source_id: e.g. "pubmed", "fda_510k"
        records:   list of validated CanonicalRecord objects

    Returns:
        Full path of the written file.
    """
    if not records:
        logger.warning("[silver] %s — no records to write", source_id)
        return ""

    now      = datetime.now(timezone.utc).replace(tzinfo=None)
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H%M%S")

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

    logger.info("[silver] %s → %d records → %s", source_id, len(records), filepath)
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