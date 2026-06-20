"""
Standalone test of GooglePatentsAdapter scoped to one country, run
outside pipeline.py's registry (which always builds the full
US+EP+WO adapter via _get_adapter()).

Mirrors pipeline.run()'s four steps. Start with WO (smallest, ~79K of
the ~404K total) to confirm the full Bronze -> normalise -> Silver
path succeeds before trying larger slices.
"""

from adapters.patents.google_patents import GooglePatentsAdapter
from normaliser.engine import NormaliserEngine
from storage import write_bronze, write_silver, print_sample

COUNTRY = ["US"]

adapter = GooglePatentsAdapter(countries=COUNTRY)

print(f"[1/4] Fetching ({COUNTRY})...")
raw, ext = adapter.fetch(since=None)
print(f"  Fetched {len(raw):,} bytes")

print("[2/4] Writing to Bronze...")
bronze_path = write_bronze(
    category=adapter.source_category,
    source_id=adapter.source_id,
    raw=raw,
    extension=ext,
)
print(f"  Bronze -> {bronze_path}")

print("[3/4] Normalising...")
engine = NormaliserEngine()
records = engine.normalise(source_id=adapter.source_id, raw=raw)
print(f"  Produced {len(records)} canonical records")

print("[4/4] Writing to Silver...")
silver_path = write_silver(
    category=adapter.source_category,
    source_id=adapter.source_id,
    records=records,
)
print(f"  Silver -> {silver_path}")

print_sample(records, n=2)