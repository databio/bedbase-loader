#!/usr/bin/env python3
"""
Monthly bulk metadata export for BEDbase.

Dumps the metadata corpus to Parquet (metadata, bedsets, bedset membership),
writes a verifiable manifest, and optionally publishes the artifacts to S3 and
records them in the ``bed_snapshots`` index table.

Design notes:

- **Sequential, not ordered.** The tables are read with plain unordered
  ``SELECT``s streamed through server-side cursors. Production BEDbase is a
  cache-starved instance (shared_buffers ~92MB vs a ~680MB working set); an
  ``ORDER BY bed.id`` scan walks the pkey in content-digest order, turning every
  row into a random heap fetch (~2 pages/row) that never stays cached — a 30-min
  hang. An unordered scan is sequential I/O and streams in seconds.
- **The metadata join happens on the runner, not the DB.** ``bed_metadata`` is
  streamed into an in-memory id→row map, then ``bed`` is streamed and merged
  against it (a LEFT JOIN in Python). This keeps the join off the constrained
  production database — it only does two sequential scans — and does not depend
  on the planner picking a good join plan.
- **The whole read is one ``REPEATABLE READ`` transaction**, so the dump is a
  consistent snapshot and the ``count(*)`` used by the integrity gate is taken
  in the same snapshot.
- Rows are streamed to disk with ``pyarrow.parquet.ParquetWriter`` (zstd). No
  table is fully materialized as Parquet in memory.
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

# Rows fetched per server-side cursor round trip / written per Parquet batch.
DEFAULT_BATCH_SIZE = 20_000

# S3 key prefix for published artifacts.
EXPORT_PREFIX = "exports"


# --------------------------------------------------------------------------- #
# Column specs. Each is (column_name, pyarrow_type). The SQL projection and the
# Parquet schema are both derived from these lists, so they cannot drift.
# The metadata export is bed + bed_metadata only; per-file stats are not
# included (they can be a separate export if needed).
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

METADATA_SCHEMA = pa.schema(BED_COLUMNS + META_COLUMNS)
BEDSET_SCHEMA = pa.schema(BEDSET_COLUMNS)
MEMBERSHIP_SCHEMA = pa.schema(MEMBERSHIP_COLUMNS)


def _bed_query() -> str:
    return "SELECT " + ", ".join(c for c, _ in BED_COLUMNS) + " FROM bed"


def _meta_query() -> str:
    return "SELECT id, " + ", ".join(c for c, _ in META_COLUMNS) + " FROM bed_metadata"


def _bedset_query() -> str:
    return "SELECT " + ", ".join(c for c, _ in BEDSET_COLUMNS) + " FROM bedsets"


MEMBERSHIP_QUERY = "SELECT bedset_id, bedfile_id FROM bedfile_bedset_relation"


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


def rows_to_table(rows, schema: pa.Schema) -> pa.Table:
    """Convert a batch of row tuples (in schema column order) to a pyarrow Table."""
    if rows:
        columns = list(zip(*rows))
    else:
        columns = [[] for _ in range(len(schema))]
    arrays = [
        pa.array(list(col), type=schema.field(i).type)
        for i, col in enumerate(columns)
    ]
    return pa.table(arrays, schema=schema)


def merge_metadata_batches(bed_batches, meta_map, n_meta_cols):
    """
    LEFT JOIN bed against bed_metadata, in Python, batch by batch.

    ``bed_batches`` yields lists of bed row tuples (id first). ``meta_map`` maps
    bed id -> tuple of metadata columns. A bed with no metadata row gets a full
    row of NULLs, matching SQL LEFT JOIN semantics.
    """
    blank = (None,) * n_meta_cols
    for batch in bed_batches:
        yield [tuple(row) + meta_map.get(row[0], blank) for row in batch]


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


def write_parquet_batches(path, schema, batch_iter) -> int:
    """
    Stream batches (lists of row tuples) into a zstd Parquet file.

    Returns the number of rows actually written (the value the manifest and the
    integrity gate must use).
    """
    rows_written = 0
    writer = pq.ParquetWriter(str(path), schema, compression="zstd")
    try:
        for batch in batch_iter:
            if not batch:
                continue
            writer.write_table(rows_to_table(batch, schema))
            rows_written += len(batch)
    finally:
        writer.close()
    return rows_written


# --------------------------------------------------------------------------- #
# Database streaming (server-side cursors; sequential, unordered scans).
# --------------------------------------------------------------------------- #


def stream_batches(conn, query: str, name: str, batch: int):
    """Yield lists of rows from a server-side cursor, ``batch`` at a time."""
    with conn.cursor(name=name) as cur:
        cur.execute(query)
        while True:
            rows = cur.fetchmany(batch)
            if not rows:
                break
            yield rows


def load_id_map(conn, query: str, name: str, batch: int) -> dict:
    """Stream ``SELECT id, ...`` into an id -> (remaining columns) dict."""
    out = {}
    for rows in stream_batches(conn, query, name, batch):
        for r in rows:
            out[r[0]] = r[1:]
    return out


def count_rows(conn, table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table}")
        return cur.fetchone()[0]


def build_exports(conn, out_dir: Path, date_str: str, batch_size: int,
                  fail_threshold: float) -> list[dict]:
    """
    Write the three Parquet files, gating each against its pre-scan count.

    Returns a list of per-file records: name, rows, bytes, sha256, file_type.
    All reads happen in the caller's REPEATABLE READ transaction.
    """
    records = []

    def finish(file_type, path, rows_written, expected):
        check_completeness(rows_written, expected, fail_threshold)
        entry = file_entry(path)
        entry["rows"] = rows_written  # rows written, never the count(*) value
        entry["file_type"] = file_type
        records.append(entry)
        _LOGGER.info(
            f"  wrote {rows_written} rows, {entry['bytes'] / 1e6:.1f} MB, "
            f"sha256={entry['sha256'][:12]}…"
        )

    # metadata = bed LEFT JOIN bed_metadata, joined on the runner.
    expected = count_rows(conn, "bed")
    fname = f"bedbase_metadata_{date_str}.parquet"
    path = out_dir / fname
    _LOGGER.info(f"Exporting metadata: ~{expected} rows -> {fname}")
    meta_map = load_id_map(conn, _meta_query(), "cur_meta_load", batch_size)
    batches = merge_metadata_batches(
        stream_batches(conn, _bed_query(), "cur_bed", batch_size),
        meta_map,
        len(META_COLUMNS),
    )
    rows_written = write_parquet_batches(path, METADATA_SCHEMA, batches)
    del meta_map  # free before the next tables
    finish("metadata", path, rows_written, expected)

    # bedsets and membership: plain single-table streaming scans.
    for file_type, schema, query, table, curname in [
        ("bedsets", BEDSET_SCHEMA, _bedset_query(), "bedsets", "cur_bedsets"),
        ("bedset_membership", MEMBERSHIP_SCHEMA, MEMBERSHIP_QUERY,
         "bedfile_bedset_relation", "cur_membership"),
    ]:
        expected = count_rows(conn, table)
        fname = f"bedbase_{file_type}_{date_str}.parquet"
        path = out_dir / fname
        _LOGGER.info(f"Exporting {file_type}: ~{expected} rows -> {fname}")
        rows_written = write_parquet_batches(
            path, schema, stream_batches(conn, query, curname, batch_size)
        )
        finish(file_type, path, rows_written, expected)

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


def run(config_path: str, out_dir: Path, batch_size: int, fail_threshold: float,
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
        records = build_exports(conn, out_dir, date_str, batch_size, fail_threshold)
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
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                        help="Rows per server-side fetch / Parquet write batch")
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
        batch_size=args.batch_size,
        fail_threshold=args.fail_threshold,
        do_publish=args.publish,
    )


if __name__ == "__main__":
    main()
