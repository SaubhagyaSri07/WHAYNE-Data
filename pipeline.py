import logging
import sys
from datetime import datetime, timedelta, timezone

from adapters.research.clinicaltrials import ClinicalTrialsAdapter
from adapters.research.pubmed import PubMedAdapter
from adapters.regulatory.fda_510k import FDA510KAdapter
from adapters.regulatory.cdsco import CDSCODevicesAdapter
from adapters.news.rss import RSSAdapter
from adapters.research.cochrane import CochraneAdapter
from adapters.regulatory.eudamed import EUDAMEDAdapter
from adapters.patents.uspto import USPTOAdapter
from adapters.trade.comtrade import ComtradeAdapter
from adapters.patents.google_patents import GooglePatentsAdapter

from normaliser.engine import NormaliserEngine
from storage import print_sample, write_bronze, write_silver

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)

engine = NormaliserEngine()


def run(source_id: str, since: datetime = None) -> dict:
    """
    Run one source end-to-end:
      1. Fetch raw data via adapter
      2. Write raw to Bronze (untouched)
      3. Normalise via engine
      4. Write canonical records to Silver

    Returns a summary dict.
    """
    print(f"\n{'=' * 60}")
    print(f"  Source   : {source_id.upper()}")
    print(f"  Run at   : {datetime.now(timezone.utc).replace(tzinfo=None).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"{'=' * 60}")

    # ── Step 1: Get adapter ───────────────────────────────────────────────
    adapter = _get_adapter(source_id)
    if not adapter:
        print(f"  ERROR: No adapter registered for '{source_id}'")
        return {"source_id": source_id, "status": "no_adapter"}

    # ── Step 2: Fetch raw data ────────────────────────────────────────────
    print(f"\n  [1/4] Fetching from source...")
    try:
        raw_content, extension = adapter.fetch(since=since)
    except Exception as e:
        logger.error("[pipeline] Fetch failed for %s: %s", source_id, e)
        return {"source_id": source_id, "status": "fetch_failed", "error": str(e)}

    if not raw_content:
        print(f"  No data returned from source.")
        return {"source_id": source_id, "status": "no_data"}

    print(f"  Fetched {len(raw_content):,} bytes")

    # ── Step 3: Write to Bronze ───────────────────────────────────────────
    print(f"\n  [2/4] Writing raw data to Bronze...")
    bronze_path = write_bronze(
        category  = adapter.source_category,
        source_id = source_id,
        raw       = raw_content,
        extension = extension,
    )
    print(f"  Bronze → {bronze_path}")

    # ── Step 4: Normalise ─────────────────────────────────────────────────
    print(f"\n  [3/4] Normalising...")
    records = engine.normalise(
        source_id = source_id,
        raw       = raw_content,
    )

    if not records:
        print(f"  No records produced by normaliser.")
        return {
            "source_id":   source_id,
            "status":      "no_records",
            "bronze_path": bronze_path,
        }

    print(f"  Produced {len(records)} canonical records")

    # ── Step 5: Write to Silver ───────────────────────────────────────────
    print(f"\n  [4/4] Writing canonical records to Silver...")
    silver_path = write_silver(
        category  = adapter.source_category,
        source_id = source_id,
        records   = records,
    )
    print(f"  Silver → {silver_path}")

    # ── Sample output ─────────────────────────────────────────────────────
    print(f"\n  Sample records:")
    print_sample(records, n=2)

    print(f"\n{'=' * 60}")
    print(f"  Done — {len(records)} records in Bronze and Silver")
    print(f"{'=' * 60}\n")

    return {
        "source_id":    source_id,
        "status":       "success",
        "record_count": len(records),
        "bronze_path":  bronze_path,
        "silver_path":  silver_path,
    }


def _get_adapter(source_id: str):
    """Return the adapter instance for a given source_id.

    Builds ONLY the requested adapter, not every registered one.
    Some adapters authenticate to external services in __init__
    (e.g. GooglePatentsAdapter -> bigquery.Client()) — eagerly
    constructing all of them meant every pipeline run depended on
    every adapter's credentials being present, even for sources
    that have nothing to do with each other.
    """
    adapter_classes = {
        "pubmed":         PubMedAdapter,
        "clinicaltrials": ClinicalTrialsAdapter,
        "fda_510k":       FDA510KAdapter,
        "cdsco":          CDSCODevicesAdapter,
        # Added as each source is built:
        "eudamed":        EUDAMEDAdapter,
        # "epo":          EPOAdapter,
        "uspto":          USPTOAdapter,
        "comtrade":       ComtradeAdapter,
        "rss":            RSSAdapter,
        "cochrane":       CochraneAdapter,
        "google_patents": GooglePatentsAdapter,
    }
    adapter_class = adapter_classes.get(source_id)
    return adapter_class() if adapter_class else None


if __name__ == "__main__":
    # Run a specific source from command line:
    #   python pipeline.py pubmed
    #   python pipeline.py clinicaltrials
    source = sys.argv[1] if len(sys.argv) > 1 else "pubmed"
    since  = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)
    run(source_id=source, since=since)