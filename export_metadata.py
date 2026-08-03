#!/usr/bin/env python3
"""
Weekly bulk metadata export for BEDbase.

Dumps the full metadata corpus to Parquet (plus bedsets and bedset membership),
writes a verifiable manifest, and optionally publishes the artifacts to S3 and
records them in the ``bed_snapshots`` index table.

Design notes:

- The metadata table is read with **keyset (seek) pagination** on the ``bed.id``
  primary key, never ``OFFSET``. Each chunk is an index descent, so the scan is
  flat with depth. PEPhub measured its OFFSET-based equivalent degrading from
  1.9s to 37.6s across the table; keyset avoids that entirely.
- The whole read runs in a single ``REPEATABLE READ`` transaction so the dump is
  a consistent snapshot. The ``count(*)`` used by the integrity gate is taken in
  the same transaction, so the gate compares like with like.
- Rows are streamed to disk with ``pyarrow.parquet.ParquetWriter`` (zstd). The
  whole table is never materialized in memory.
- The manifest's ``rows`` field is the count of rows actually written, not the
  value ``count(*)`` returned. Recording the live count here was the PEPhub bug
  that made a short dump undetectable.

Standalone, in the style of ``update_stats.py``. Reads ``config.yaml``
(env-var interpolated) for the Postgres DSN and S3 settings.
"""

import argparse
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import yaml

_LOGGER = logging.getLogger("bedbase-export")

# Bump when the exported column set or types change in a breaking way.
SCHEMA_VERSION = 1

# Default keyset chunk size. 50k rows per index descent is a good balance of
# round-trips against per-chunk memory.
DEFAULT_CHUNK_SIZE = 50_000

# S3 key prefix for published artifacts.
EXPORT_PREFIX = "exports"


# --------------------------------------------------------------------------- #
# Column specs. Each is (column_name, pyarrow_type). The SQL projection and the
# Parquet schema are both derived from these lists, so they cannot drift.
# --------------------------------------------------------------------------- #

_TS = pa.timestamp("us", tz="UTC")

BED_COLUMNS = [
    ("id", pa.string()),
    ("name", pa.string()),
    ("genome_alias", pa.string()),
    ("genome_digest", pa.string()),
    ("description", pa.string()),
    ("bed_compliance", pa.string()),
    ("data_format", pa.string()),
    ("compliant_columns", pa.int64()),
    ("non_compliant_columns", pa.int64()),
    ("header", pa.string()),
    ("indexed", pa.bool_()),
    ("file_indexed", pa.bool_()),
    ("pephub", pa.bool_()),
    ("submission_date", _TS),
    ("last_update_date", _TS),
    ("is_universe", pa.bool_()),
    ("license_id", pa.string()),
    ("processed", pa.bool_()),
]

META_COLUMNS = [
    ("species_name", pa.string()),
    ("species_id", pa.string()),
    ("genotype", pa.string()),
    ("phenotype", pa.string()),
    ("cell_type", pa.string()),
    ("cell_line", pa.string()),
    ("tissue", pa.string()),
    ("library_source", pa.string()),
    ("assay", pa.string()),
    ("antibody", pa.string()),
    ("target", pa.string()),
    ("treatment", pa.string()),
    ("original_file_name", pa.string()),
    ("global_sample_id", pa.list_(pa.string())),
    ("global_experiment_id", pa.list_(pa.string())),
]

STATS_COLUMNS = [
    ("number_of_regions", pa.float64()),
    ("gc_content", pa.float64()),
    ("median_tss_dist", pa.float64()),
    ("mean_region_width", pa.float64()),
    ("exon_frequency", pa.float64()),
    ("intron_frequency", pa.float64()),
    ("promoterprox_frequency", pa.float64()),
    ("intergenic_frequency", pa.float64()),
    ("promotercore_frequency", pa.float64()),
    ("fiveutr_frequency", pa.float64()),
    ("threeutr_frequency", pa.float64()),
    ("fiveutr_percentage", pa.float64()),
    ("threeutr_percentage", pa.float64()),
    ("promoterprox_percentage", pa.float64()),
    ("exon_percentage", pa.float64()),
    ("intron_percentage", pa.float64()),
    ("intergenic_percentage", pa.float64()),
    ("promotercore_percentage", pa.float64()),
    ("tssdist", pa.float64()),
]

BEDSET_COLUMNS = [
    ("id", pa.string()),
    ("name", pa.string()),
    ("description", pa.string()),
    ("summary", pa.string()),
    ("submission_date", _TS),
    ("last_update_date", _TS),
    ("md5sum", pa.string()),
    ("author", pa.string()),
    ("source", pa.string()),
    ("processed", pa.bool_()),
]

MEMBERSHIP_COLUMNS = [
    ("bedset_id", pa.string()),
    ("bedfile_id", pa.string()),
]

METADATA_SCHEMA = pa.schema(BED_COLUMNS + META_COLUMNS + STATS_COLUMNS)
BEDSET_SCHEMA = pa.schema(BEDSET_COLUMNS)
MEMBERSHIP_SCHEMA = pa.schema(MEMBERSHIP_COLUMNS)


def _metadata_query() -> str:
    cols = (
        [f"b.{c}" for c, _ in BED_COLUMNS]
        + [f"m.{c}" for c, _ in META_COLUMNS]
        + [f"s.{c}" for c, _ in STATS_COLUMNS]
    )
    return f"""
        SELECT {", ".join(cols)}
        FROM bed b
        LEFT JOIN bed_metadata m ON b.id = m.id
        LEFT JOIN bed_stats s ON b.id = s.id
        WHERE (%(cursor)s::text IS NULL OR b.id > %(cursor)s::text)
        ORDER BY b.id
        LIMIT %(limit)s
    """


def _bedset_query() -> str:
    cols = [c for c, _ in BEDSET_COLUMNS]
    return f"""
        SELECT {", ".join(cols)}
        FROM bedsets
        WHERE (%(cursor)s::text IS NULL OR id > %(cursor)s::text)
        ORDER BY id
        LIMIT %(limit)s
    """


# Composite keyset on the (bedset_id, bedfile_id) primary key. The row-value
# comparison uses the PK btree index directly.
MEMBERSHIP_QUERY = """
    SELECT bedset_id, bedfile_id
    FROM bedfile_bedset_relation
    WHERE (%(a)s::text IS NULL OR (bedset_id, bedfile_id) > (%(a)s::text, %(b)s::text))
    ORDER BY bedset_id, bedfile_id
    LIMIT %(limit)s
"""


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested offline, no DB, no network).
# --------------------------------------------------------------------------- #


def _expand_env(obj):
    """Recursively expand ``$VAR`` / ``${VAR}`` in every string value."""
    if isinstance(obj, str):
        return os.path.expandvars(obj)
    if isinstance(obj, dict):
        return {k: _expand_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env(v) for v in obj]
    return obj


def load_config(path: str) -> dict:
    """
    Load a YAML config, interpolating ``$VAR`` / ``${VAR}`` from the env.

    Expansion happens *after* parsing, on individual string values, not on the
    raw file text. A secret substituted into the text before parsing can contain
    YAML metacharacters (``>``, ``|``, ``:``, ``#`` …) and break the parser; a
    real Postgres password did exactly that.
    """
    data = yaml.safe_load(Path(path).read_text())
    return _expand_env(data)


def keyset_pages(fetch_page, limit: int, start_cursor=None):
    """
    Yield successive pages from a keyset-paginated source.

    ``fetch_page(cursor, limit)`` returns ``(rows, next_cursor)``. Iteration
    stops on the first page shorter than ``limit`` (an empty page ends it too,
    which is what makes a boundary landing exactly on ``limit`` terminate
    without emitting duplicates).
    """
    cursor = start_cursor
    while True:
        rows, cursor = fetch_page(cursor, limit)
        if not rows:
            break
        yield rows
        if len(rows) < limit:
            break


def rows_to_table(rows, schema: pa.Schema) -> pa.Table:
    """Convert a page of row tuples (in schema column order) to a pyarrow Table."""
    if rows:
        columns = list(zip(*rows))
    else:
        columns = [[] for _ in range(len(schema))]
    arrays = [
        pa.array(list(col), type=schema.field(i).type)
        for i, col in enumerate(columns)
    ]
    return pa.table(arrays, schema=schema)


def check_completeness(rows_written: int, expected_count: int, threshold: float):
    """
    Refuse to publish a partial artifact.

    Raises if the fraction of rows missing versus the pre-scan ``count(*)``
    exceeds ``threshold``. Extra rows (concurrent ingest) never trip it.
    """
    if expected_count <= 0:
        return
    shortfall = (expected_count - rows_written) / expected_count
    if shortfall > threshold:
        raise RuntimeError(
            f"Wrote {rows_written} rows but expected ~{expected_count} "
            f"(short by {shortfall:.2%} > {threshold:.2%}). "
            f"Refusing to publish a partial artifact."
        )


def sha256_file(path, chunk: int = 1 << 20) -> str:
    """Streaming SHA256 of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def file_entry(path) -> dict:
    """Manifest entry describing a written file: name, bytes, sha256."""
    path = Path(path)
    return {
        "name": path.name,
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


# --------------------------------------------------------------------------- #
# Export orchestration.
# --------------------------------------------------------------------------- #


def export_parquet(path, schema, fetch_page, chunk_size) -> int:
    """
    Stream a keyset-paginated source into a zstd Parquet file.

    Returns the number of rows actually written (the value the manifest and the
    integrity gate must use).
    """
    rows_written = 0
    writer = pq.ParquetWriter(str(path), schema, compression="zstd")
    try:
        for page in keyset_pages(fetch_page, chunk_size):
            writer.write_table(rows_to_table(page, schema))
            rows_written += len(page)
    finally:
        writer.close()
    return rows_written


def make_single_key_fetch(conn, query: str, key_index: int = 0):
    """Build a fetch_page for a single-column string cursor."""

    def fetch(cursor, limit):
        with conn.cursor() as cur:
            cur.execute(query, {"cursor": cursor, "limit": limit})
            rows = cur.fetchall()
        next_cursor = rows[-1][key_index] if rows else cursor
        return rows, next_cursor

    return fetch


def make_membership_fetch(conn):
    """Build a fetch_page for the composite (bedset_id, bedfile_id) cursor."""

    def fetch(cursor, limit):
        a, b = (None, None) if cursor is None else cursor
        with conn.cursor() as cur:
            cur.execute(MEMBERSHIP_QUERY, {"a": a, "b": b, "limit": limit})
            rows = cur.fetchall()
        next_cursor = (rows[-1][0], rows[-1][1]) if rows else cursor
        return rows, next_cursor

    return fetch


def count_rows(conn, table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table}")
        return cur.fetchone()[0]


def build_exports(conn, out_dir: Path, date_str: str, chunk_size: int,
                  fail_threshold: float) -> list[dict]:
    """
    Write the three Parquet files, gating each against its pre-scan count.

    Returns a list of per-file records: name, rows, bytes, sha256, file_type.
    All reads happen in the caller's REPEATABLE READ transaction.
    """
    specs = [
        ("metadata", METADATA_SCHEMA,
         make_single_key_fetch(conn, _metadata_query()), "bed"),
        ("bedsets", BEDSET_SCHEMA,
         make_single_key_fetch(conn, _bedset_query()), "bedsets"),
        ("bedset_membership", MEMBERSHIP_SCHEMA,
         make_membership_fetch(conn), "bedfile_bedset_relation"),
    ]

    records = []
    for file_type, schema, fetch, table in specs:
        expected = count_rows(conn, table)
        filename = f"bedbase_{file_type}_{date_str}.parquet"
        path = out_dir / filename
        _LOGGER.info(f"Exporting {file_type}: ~{expected} rows -> {filename}")
        rows_written = export_parquet(path, schema, fetch, chunk_size)
        check_completeness(rows_written, expected, fail_threshold)
        entry = file_entry(path)
        entry["rows"] = rows_written  # rows written, never the count(*) value
        entry["file_type"] = file_type
        records.append(entry)
        _LOGGER.info(
            f"  wrote {rows_written} rows, {entry['bytes'] / 1e6:.1f} MB, "
            f"sha256={entry['sha256'][:12]}…"
        )
    return records


def write_manifest(out_dir: Path, date_str: str, records: list[dict],
                   started: str, ended: str, source_db: str) -> dict:
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "build_started": started,
        "build_ended": ended,
        "source_database": source_db,
        "files": [
            {k: r[k] for k in ("name", "file_type", "rows", "bytes", "sha256")}
            for r in records
        ],
    }
    path = out_dir / f"manifest_{date_str}.json"
    path.write_text(json.dumps(manifest, indent=2))
    manifest["_path"] = str(path)
    return manifest


# --------------------------------------------------------------------------- #
# Publication: S3 upload + snapshot index rows.
# --------------------------------------------------------------------------- #

# Canonical schema lives in bbconf.db_utils.BedSnapshot; this mirrors it so the
# exporter can run against a database whose API has not yet created the table.
# Keep the two in sync.
_ENSURE_SNAPSHOT_TABLE = """
    CREATE TABLE IF NOT EXISTS bed_snapshots (
        id SERIAL PRIMARY KEY,
        file_path VARCHAR NOT NULL,
        file_type VARCHAR NOT NULL,
        creation_date TIMESTAMP WITH TIME ZONE NOT NULL,
        record_count INTEGER,
        file_size INTEGER,
        checksum VARCHAR,
        schema_version INTEGER
    )
"""

_INSERT_SNAPSHOT = """
    INSERT INTO bed_snapshots
        (file_path, file_type, creation_date, record_count,
         file_size, checksum, schema_version)
    VALUES (%(file_path)s, %(file_type)s, %(creation_date)s, %(record_count)s,
            %(file_size)s, %(checksum)s, %(schema_version)s)
"""


def publish(conn, out_dir: Path, records: list[dict], manifest: dict,
            bucket: str, endpoint_url: str, creation_date: datetime):
    """Upload every artifact to S3, then record a row per file in bed_snapshots."""
    import upload_s3

    # Upload the data files and the manifest.
    to_upload = list(records) + [
        {"name": Path(manifest["_path"]).name, "file_type": "manifest",
         "rows": None, "bytes": Path(manifest["_path"]).stat().st_size,
         "sha256": sha256_file(manifest["_path"])}
    ]

    for rec in to_upload:
        key = f"{EXPORT_PREFIX}/{rec['name']}"
        local = out_dir / rec["name"]
        upload_s3.upload_file(str(local), bucket, key, endpoint_url=endpoint_url)
        rec["_key"] = key

    # Record the index rows only after every upload has succeeded.
    with conn.cursor() as cur:
        cur.execute(_ENSURE_SNAPSHOT_TABLE)
        for rec in to_upload:
            cur.execute(_INSERT_SNAPSHOT, {
                "file_path": rec["_key"],
                "file_type": rec["file_type"],
                "creation_date": creation_date,
                "record_count": rec["rows"],
                "file_size": rec["bytes"],
                "checksum": rec["sha256"],
                "schema_version": SCHEMA_VERSION,
            })
    conn.commit()
    _LOGGER.info(f"Recorded {len(to_upload)} rows in bed_snapshots")


def run(config_path: str, out_dir: Path, chunk_size: int, fail_threshold: float,
        do_publish: bool):
    import psycopg
    from psycopg import IsolationLevel

    cfg = load_config(config_path)
    db = cfg["database"]
    s3 = cfg.get("s3", {})

    out_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y_%m_%d")
    started = now.isoformat()

    conn = psycopg.connect(
        host=db["host"],
        port=db.get("port", 5432),
        dbname=db.get("database", "bedbase"),
        user=db["user"],
        password=db["password"],
    )
    try:
        # Consistent read snapshot: counts and the full scan share one snapshot.
        conn.isolation_level = IsolationLevel.REPEATABLE_READ
        records = build_exports(conn, out_dir, date_str, chunk_size, fail_threshold)
        conn.commit()  # end the read transaction before slow uploads

        ended = datetime.now(timezone.utc).isoformat()
        manifest = write_manifest(
            out_dir, date_str, records, started, ended, db.get("database", "bedbase")
        )
        _LOGGER.info(f"Manifest: {manifest['_path']}")

        if not do_publish:
            _LOGGER.info("publish=false; artifacts left on disk, nothing uploaded.")
            return

        conn.isolation_level = IsolationLevel.READ_COMMITTED
        publish(
            conn, out_dir, records, manifest,
            bucket=s3.get("bucket", "bedbase"),
            endpoint_url=s3.get("endpoint_url") or os.getenv("AWS_ENDPOINT_URL"),
            creation_date=now,
        )
        for rec in records:
            url = f"https://data2.bedbase.org/{rec['_key']}"
            _LOGGER.info(f"Published {url}")
    finally:
        conn.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml",
                        help="Path to config.yaml (env-var interpolated)")
    parser.add_argument("--output-dir", default="exports",
                        help="Local directory for the built artifacts")
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE,
                        help="Keyset chunk size (rows per query)")
    parser.add_argument("--fail-threshold", type=float, default=0.01,
                        help="Abort without publishing if this fraction of rows is missing")
    pub = parser.add_mutually_exclusive_group()
    pub.add_argument("--publish", dest="publish", action="store_true",
                     help="Upload to S3 and record the snapshot index rows")
    pub.add_argument("--no-publish", dest="publish", action="store_false",
                     help="Dry run: build artifacts locally, upload nothing")
    parser.set_defaults(publish=False)
    args = parser.parse_args()

    run(
        config_path=args.config,
        out_dir=Path(args.output_dir),
        chunk_size=args.chunk_size,
        fail_threshold=args.fail_threshold,
        do_publish=args.publish,
    )


if __name__ == "__main__":
    main()
