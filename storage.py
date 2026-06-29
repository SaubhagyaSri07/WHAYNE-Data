import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3
import pyarrow as pa
from botocore.config import Config
from dotenv import load_dotenv
from pyiceberg.catalog import load_catalog
from pyiceberg.exceptions import NoSuchTableError
from pyiceberg.expressions import And, EqualTo, In
from pyiceberg.partitioning import PartitionField, PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.transforms import DayTransform, IdentityTransform
from pyiceberg.types import (
    NestedField,
    StringType,
    TimestampType,
)

from schema import CanonicalRecord

# Load variables from .env in the project root (no-op if the file
# doesn't exist, e.g. in a deployed environment using real env vars).
load_dotenv()

logger = logging.getLogger(__name__)


# ── Shared R2 configuration ───────────────────────────────────────────────────
# Bronze and Silver both live on the same Cloudflare R2 account.
# Only the bucket name differs between layers.
#
# Set before running the pipeline (.env or PowerShell):
#   R2_ACCOUNT_ID        = <account id from Cloudflare dashboard>
#   R2_ACCESS_KEY_ID     = <R2 API token access key>
#   R2_SECRET_ACCESS_KEY = <R2 API token secret key>
#
# Cloudflare Dashboard → R2 → Overview (Account ID top-right)
# R2 → Manage R2 API Tokens → Create Token (Object Read & Write on both buckets)
R2_ACCOUNT_ID        = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID     = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")

# R2 endpoint — derived from account ID, same for all buckets on the account
R2_ENDPOINT = f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"


# ── Bronze configuration ──────────────────────────────────────────────────────
R2_BRONZE_BUCKET  = os.environ.get("R2_BRONZE_BUCKET", "wine")
BRONZE_KEY_PREFIX = "bronze"


# ── Silver configuration ──────────────────────────────────────────────────────
# Silver bucket confirmed: wine-silver
# Endpoint: https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com/wine-silver
#
# Additional env vars required for Silver:
#   SILVER_CATALOG_URI       = postgresql://user:pass@host.neon.tech/dbname
#   SILVER_CATALOG_NAMESPACE = whayne          (Iceberg namespace, one per project)
#   SILVER_CATALOG_TABLE     = silver          (Iceberg table name)
#   R2_SILVER_BUCKET         = wine-silver     (already created on R2)
R2_SILVER_BUCKET         = os.environ.get("R2_SILVER_BUCKET", "wine-silver")
SILVER_CATALOG_URI       = os.environ.get("SILVER_CATALOG_URI", "")
SILVER_CATALOG_NAMESPACE = os.environ.get("SILVER_CATALOG_NAMESPACE", "whayne")
SILVER_CATALOG_TABLE     = os.environ.get("SILVER_CATALOG_TABLE", "silver")

# Fully qualified Iceberg table identifier: whayne.silver
SILVER_TABLE_ID = f"{SILVER_CATALOG_NAMESPACE}.{SILVER_CATALOG_TABLE}"


# ── Iceberg schema — mirrors CanonicalRecord exactly ─────────────────────────
# Rules:
#   - Field IDs are permanent — never renumber or reuse once the table exists
#   - Add new fields at the end only (Iceberg schema evolution is additive)
#   - Complex types (actors, tags, classifiers, lineage) → JSON strings
#     so DuckDB can query them with json_extract() without nested struct columns
SILVER_SCHEMA = Schema(
    NestedField(1,  "record_id",      StringType(),    required=True),
    NestedField(2,  "source_id",      StringType(),    required=True),
    NestedField(3,  "source_type",    StringType(),    required=True),
    NestedField(4,  "source_url",     StringType(),    required=True),
    NestedField(5,  "external_id",    StringType(),    required=False),
    NestedField(6,  "captured_at",    TimestampType(), required=True),
    NestedField(7,  "published_at",   TimestampType(), required=False),
    NestedField(8,  "entity_type",    StringType(),    required=True),
    NestedField(9,  "title",          StringType(),    required=True),
    NestedField(10, "summary",        StringType(),    required=False),
    NestedField(11, "region",         StringType(),    required=False),
    NestedField(12, "language",       StringType(),    required=True),
    NestedField(13, "actors",         StringType(),    required=False),  # JSON → List[Actor]
    NestedField(14, "tags",           StringType(),    required=False),  # JSON → List[str]
    NestedField(15, "classifiers",    StringType(),    required=True),   # JSON → Dict[str, Any]
    NestedField(16, "content_hash",   StringType(),    required=True),   # dedup key (UNIQUE)
    NestedField(17, "schema_version", StringType(),    required=True),
    NestedField(18, "lineage",        StringType(),    required=False),  # JSON → Lineage
)

# Partition by source_id + day(captured_at)
# Mirrors Bronze key structure → replay from Bronze to Silver stays aligned
# Partition field IDs start at 1000 to avoid collision with schema field IDs
SILVER_PARTITION_SPEC = PartitionSpec(
    PartitionField(
        source_id=2,      # references schema field 2: source_id
        field_id=1000,
        transform=IdentityTransform(),
        name="source_id",
    ),
    PartitionField(
        source_id=6,      # references schema field 6: captured_at
        field_id=1001,
        transform=DayTransform(),
        name="captured_at_day",
    ),
)


# ─────────────────────────────────────────────────────────────────────────────
# INTERNAL HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _r2_client():
    """
    Build a boto3 S3 client pointed at Cloudflare R2 (Bronze).
    Called per-operation so missing env vars raise at point of use.
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
            f"[bronze/R2] Missing env vars: {missing}. "
            f"Set them in .env or PowerShell before running."
        )

    return boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        config=Config(signature_version="s3v4"),
        region_name="auto",   # R2 requires "auto"
    )


def _silver_catalog():
    """
    Build a PyIceberg SQL catalog backed by Neon Postgres.

    The catalog tracks Iceberg table metadata (schema, snapshots, partitions).
    Actual Parquet data files are written to R2 (wine-silver bucket).

    Requires SILVER_CATALOG_URI — set it to your Neon connection string:
        postgresql://user:pass@host.neon.tech/dbname
    """
    if not SILVER_CATALOG_URI:
        raise EnvironmentError(
            "[silver/catalog] SILVER_CATALOG_URI not set. "
            "Set it to your Neon Postgres connection string:\n"
            "  postgresql://user:pass@host.neon.tech/dbname\n"
            "Sign up free at https://neon.tech"
        )

    missing_r2 = [
        name for name, val in [
            ("R2_ACCOUNT_ID",        R2_ACCOUNT_ID),
            ("R2_ACCESS_KEY_ID",     R2_ACCESS_KEY_ID),
            ("R2_SECRET_ACCESS_KEY", R2_SECRET_ACCESS_KEY),
        ] if not val
    ]
    if missing_r2:
        raise EnvironmentError(
            f"[silver/R2] Missing env vars for Silver R2 storage: {missing_r2}."
        )

    return load_catalog(
        "default",
        **{
            # Catalog type: SQL (JDBC) backed by Postgres
            "type":                   "sql",
            "uri":                    SILVER_CATALOG_URI,

            # Warehouse root — where Iceberg writes Parquet files on R2
            # Table files land at: s3://wine-silver/whayne/silver/data/
            "warehouse":              f"s3://{R2_SILVER_BUCKET}/",

            # Use PyArrow's built-in S3FileSystem for R2 file I/O.
            # Avoids s3fs which pulls in aiobotocore — a package with a
            # strict boto3 version pin that conflicts with boto3>=1.34.
            "io-impl":                "pyiceberg.io.pyarrow.PyArrowFileIO",

            # R2 S3-compatible storage config
            # Same account and credentials as Bronze — no new creds needed
            "s3.endpoint":            R2_ENDPOINT,
            "s3.access-key-id":       R2_ACCESS_KEY_ID,
            "s3.secret-access-key":   R2_SECRET_ACCESS_KEY,
            # R2 ignores region but PyArrowFileIO requires a non-"auto" string
            "s3.region":              "us-east-1",
            "s3.path-style-access":   "true",
        },
    )


def _ensure_silver_table():
    """
    Load the Silver Iceberg table, creating it (and its namespace) if
    it doesn't exist.  Idempotent — safe to call on every write.

    Catalog:  Neon Postgres  (whayne.silver)
    Storage:  R2             (s3://wine-silver/whayne/silver/)
    """
    catalog = _silver_catalog()

    try:
        return catalog.load_table(SILVER_TABLE_ID)

    except NoSuchTableError:
        logger.info(
            "[silver/iceberg] Table '%s' not found — creating it now",
            SILVER_TABLE_ID,
        )

        # Create the Iceberg namespace (whayne) if it doesn't exist
        try:
            catalog.create_namespace(SILVER_CATALOG_NAMESPACE)
            logger.info(
                "[silver/iceberg] Created namespace: %s",
                SILVER_CATALOG_NAMESPACE,
            )
        except Exception:
            pass   # Namespace already exists — safe to ignore

        return catalog.create_table(
            identifier=SILVER_TABLE_ID,
            schema=SILVER_SCHEMA,
            partition_spec=SILVER_PARTITION_SPEC,
            location=f"s3://{R2_SILVER_BUCKET}/{SILVER_CATALOG_NAMESPACE}/{SILVER_CATALOG_TABLE}",
        )


def _records_to_arrow(records: List[CanonicalRecord]) -> pa.Table:
    """
    Convert CanonicalRecord list to a PyArrow Table for Iceberg append.

    Complex types are serialised to JSON strings so DuckDB can query
    them with json_extract() without requiring nested Iceberg columns.

    An explicit PyArrow schema is provided so that:
      - nullable=False matches Iceberg required=True fields exactly
        (PyIceberg 0.10+ rejects inferred optional on required fields)
      - region is always pa.string() even when all batch values are None
        (PyArrow infers pa.null() / "unknown" for all-None columns)
      - timestamps are always pa.timestamp("us") matching Iceberg TimestampType
    """
    # Mirrors SILVER_SCHEMA nullability exactly.
    # nullable=False  ↔  required=True  in SILVER_SCHEMA
    # nullable=True   ↔  required=False in SILVER_SCHEMA
    ARROW_SCHEMA = pa.schema([
        pa.field("record_id",      pa.string(),           nullable=False),
        pa.field("source_id",      pa.string(),           nullable=False),
        pa.field("source_type",    pa.string(),           nullable=False),
        pa.field("source_url",     pa.string(),           nullable=False),
        pa.field("external_id",    pa.string(),           nullable=True),
        pa.field("captured_at",    pa.timestamp("us"),    nullable=False),
        pa.field("published_at",   pa.timestamp("us"),    nullable=True),
        pa.field("entity_type",    pa.string(),           nullable=False),
        pa.field("title",          pa.string(),           nullable=False),
        pa.field("summary",        pa.string(),           nullable=True),
        pa.field("region",         pa.string(),           nullable=True),
        pa.field("language",       pa.string(),           nullable=False),
        pa.field("actors",         pa.string(),           nullable=True),
        pa.field("tags",           pa.string(),           nullable=True),
        pa.field("classifiers",    pa.string(),           nullable=False),
        pa.field("content_hash",   pa.string(),           nullable=False),
        pa.field("schema_version", pa.string(),           nullable=False),
        pa.field("lineage",        pa.string(),           nullable=True),
    ])

    rows = []
    for r in records:
        rows.append({
            "record_id":      r.record_id,
            "source_id":      r.source_id,
            "source_type":    r.source_type,
            "source_url":     r.source_url,
            "external_id":    r.external_id,
            "captured_at":    r.captured_at,
            "published_at":   r.published_at,
            "entity_type":    r.entity_type.value,
            "title":          r.title,
            "summary":        r.summary,
            "region":         r.region,
            "language":       r.language,
            "actors":         json.dumps([a.model_dump() for a in r.actors], default=str),
            "tags":           json.dumps(r.tags),
            "classifiers":    json.dumps(r.classifiers, default=str),
            "content_hash":   r.content_hash,
            "schema_version": r.schema_version,
            "lineage":        json.dumps(r.lineage.model_dump(), default=str)
                              if r.lineage else None,
        })

    return pa.Table.from_pylist(rows, schema=ARROW_SCHEMA)


def _existing_hashes(table, hashes: List[str]) -> set:
    """
    Scan the Iceberg table for content_hashes that already exist.
    Reads only the content_hash column — no full row scan.
    Returns empty set on any scan error (safe: worst case we attempt
    a write and Iceberg rejects the duplicate at commit time).
    """
    if not hashes:
        return set()

    try:
        result = table.scan(
            row_filter=In("content_hash", hashes),
            selected_fields=("content_hash",),
        ).to_arrow()
        return set(result["content_hash"].to_pylist())
    except Exception as e:
        logger.warning(
            "[silver/iceberg] Hash scan failed — will attempt write anyway: %s", e
        )
        return set()


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
    Upload raw source response to Bronze (Cloudflare R2, whayne-bronze).

    Args:
        category:  e.g. "research", "regulatory", "patents"
        source_id: e.g. "pubmed", "fda_510k"
        raw:       raw response string exactly as received from source
        extension: file extension — json, xml, html

    Returns:
        R2 object key, e.g. bronze/research/pubmed/2025-01-15/raw_143022.json
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
    """Download and return raw content from a Bronze R2 object."""
    response = _r2_client().get_object(Bucket=R2_BRONZE_BUCKET, Key=key)
    return response["Body"].read().decode("utf-8")


def list_bronze(
    category:  str,
    source_id: str,
    date:      Optional[str] = None,
) -> List[str]:
    """
    List Bronze object keys for a source.
    Pass date (YYYY-MM-DD) to scope to a single day.
    """
    prefix = f"{BRONZE_KEY_PREFIX}/{category}/{source_id}/"
    if date:
        prefix += f"{date}/"

    client    = _r2_client()
    paginator = client.get_paginator("list_objects_v2")
    keys      = []

    for page in paginator.paginate(Bucket=R2_BRONZE_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            keys.append(obj["Key"])

    return sorted(keys)


# ─────────────────────────────────────────────────────────────────────────────
# SILVER  — canonical, normalised records → Iceberg on R2 (wine-silver)
#           Catalog: Neon Postgres (JDBC/SQL)
# ─────────────────────────────────────────────────────────────────────────────

def write_silver(
    category:  str,           # kept for pipeline.py compatibility — not used in Iceberg path
    source_id: str,
    records:   List[CanonicalRecord],
) -> str:
    """
    Write normalised canonical records to Silver (Iceberg on R2, via Neon catalog).

    Deduplication:
        Scans existing content_hashes before each write.
        Only records with a new hash are appended.
        This gives idempotent, exactly-once Silver records without MERGE INTO.

    Args:
        category:  unused in Iceberg path (partitioned by source_id + day).
                   Kept so pipeline.py call site needs no changes.
        source_id: e.g. "pubmed", "fda_510k"
        records:   validated CanonicalRecord objects from the normaliser

    Returns:
        Iceberg table identifier, e.g. "whayne.silver"
    """
    if not records:
        logger.warning("[silver] %s — no records to write", source_id)
        return "", 0

    table      = _ensure_silver_table()
    all_hashes = [r.content_hash for r in records]

    # ── Deduplication ────────────────────────────────────────────────────────
    existing    = _existing_hashes(table, all_hashes)
    new_records = [r for r in records if r.content_hash not in existing]

    skipped = len(records) - len(new_records)
    if skipped:
        logger.info(
            "[silver] %s — %d duplicate(s) skipped", source_id, skipped
        )

    if not new_records:
        logger.info("[silver] %s — all records already in Silver", source_id)
        return SILVER_TABLE_ID, 0

    # ── Write ────────────────────────────────────────────────────────────────
    arrow_table = _records_to_arrow(new_records)
    table.append(arrow_table)

    logger.info(
        "[silver/iceberg] %s → %d new records → %s",
        source_id, len(new_records), SILVER_TABLE_ID,
    )
    return SILVER_TABLE_ID, len(new_records)


def read_silver(
    source_id:   Optional[str] = None,
    entity_type: Optional[str] = None,
    limit:       int = 100,
) -> List[Dict[str, Any]]:
    """
    Read records from Silver Iceberg table.

    Args:
        source_id:   filter to one source, e.g. "pubmed"
        entity_type: filter to one entity type, e.g. "research_paper"
        limit:       max rows to return

    Returns:
        List of row dicts. JSON string columns (actors, classifiers, tags,
        lineage) are returned as strings — call json.loads() on them as needed.
    """
    table = _ensure_silver_table()

    if source_id and entity_type:
        row_filter = And(
            EqualTo("source_id", source_id),
            EqualTo("entity_type", entity_type),
        )
    elif source_id:
        row_filter = EqualTo("source_id", source_id)
    elif entity_type:
        row_filter = EqualTo("entity_type", entity_type)
    else:
        row_filter = None

    scan = (
        table.scan(row_filter=row_filter, limit=limit)
        if row_filter
        else table.scan(limit=limit)
    )

    return scan.to_arrow().to_pylist()


def list_silver(
    source_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    List Silver Iceberg table snapshots.

    Each snapshot corresponds to one successful write_silver() call.
    Useful for audit, replay planning, and time-travel queries.

    Returns:
        Sorted list of dicts with snapshot_id, timestamp, operation.
    """
    table     = _ensure_silver_table()
    snapshots = table.snapshots()

    result = [
        {
            "snapshot_id": s.snapshot_id,
            "timestamp":   datetime.fromtimestamp(
                               s.timestamp_ms / 1000, tz=timezone.utc
                           ).isoformat(),
            "operation":   s.summary.get("operation", "unknown")
                           if s.summary else "unknown",
        }
        for s in snapshots
    ]

    if source_id:
        logger.info(
            "[silver/iceberg] Snapshots are table-wide. "
            "Use read_silver(source_id='%s') to query records for one source.",
            source_id,
        )

    return sorted(result, key=lambda x: x["timestamp"])


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