"""Offline tests for the metadata exporter. No database, no network."""

import json
import os
import sys

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import export_metadata as em  # noqa: E402


# --------------------------------------------------------------------------- #
# A stubbed keyset source: known rows, paged by an integer id cursor.
# --------------------------------------------------------------------------- #


def make_fetch(total):
    """Return a fetch_page over ids 1..total, keyset-paginated on the id."""
    data = [(i, f"name{i}") for i in range(1, total + 1)]

    def fetch(cursor, limit):
        start = 0 if cursor is None else cursor  # ids are 1-based; cursor is last id
        page = [row for row in data if row[0] > start][:limit]
        next_cursor = page[-1][0] if page else cursor
        return page, next_cursor

    return fetch, data


class TestKeysetChunker:
    def test_full_coverage_no_dupes(self):
        fetch, data = make_fetch(23)
        seen = [row for page in em.keyset_pages(fetch, 5) for row in page]
        assert seen == data
        assert len(seen) == len({r[0] for r in seen})  # no duplicates

    def test_boundary_exact_multiple(self):
        """A chunk boundary landing exactly on the last row must still terminate."""
        fetch, data = make_fetch(100)
        pages = list(em.keyset_pages(fetch, 50))
        assert [len(p) for p in pages] == [50, 50]
        assert [row for page in pages for row in page] == data

    def test_single_short_page(self):
        fetch, data = make_fetch(3)
        pages = list(em.keyset_pages(fetch, 50))
        assert len(pages) == 1 and pages[0] == data

    def test_empty_source(self):
        fetch, _ = make_fetch(0)
        assert list(em.keyset_pages(fetch, 10)) == []


class TestParquetRoundTrip:
    def test_schema_and_dtypes_survive(self, tmp_path):
        schema = pa.schema([
            ("id", pa.string()),
            ("n", pa.int64()),
            ("flag", pa.bool_()),
            ("val", pa.float64()),
            ("tags", pa.list_(pa.string())),
        ])
        rows = [
            ("a" * 32, 5, True, 1.5, ["GSM1", "GSM2"]),
            ("b" * 32, 7, False, 2.5, []),
        ]
        path = tmp_path / "rt.parquet"
        fetch = lambda cur, lim: (rows if cur is None else [], rows[-1][0])  # noqa: E731
        written = em.export_parquet(path, schema, fetch, chunk_size=10)
        assert written == 2

        table = pq.read_table(path)
        assert table.schema == schema
        back = table.to_pylist()
        assert back[0]["id"] == "a" * 32
        assert back[0]["tags"] == ["GSM1", "GSM2"]
        assert back[1]["flag"] is False

    def test_zstd_compression_used(self, tmp_path):
        schema = pa.schema([("id", pa.string())])
        rows = [(f"id{i}",) for i in range(100)]
        path = tmp_path / "z.parquet"
        fetch = lambda cur, lim: (rows if cur is None else [], rows[-1][0])  # noqa: E731
        em.export_parquet(path, schema, fetch, chunk_size=1000)
        meta = pq.ParquetFile(path).metadata.row_group(0).column(0)
        assert meta.compression == "ZSTD"


class TestFailureGate:
    def test_raises_when_short_beyond_threshold(self):
        with pytest.raises(RuntimeError, match="partial artifact"):
            em.check_completeness(rows_written=900, expected_count=1000, threshold=0.01)

    def test_does_not_raise_just_inside_threshold(self):
        # 5 short out of 1000 = 0.5% < 1%
        em.check_completeness(rows_written=995, expected_count=1000, threshold=0.01)

    def test_extra_rows_never_trip_it(self):
        # Concurrent ingest can add rows; that must not abort.
        em.check_completeness(rows_written=1010, expected_count=1000, threshold=0.01)

    def test_zero_expected_is_noop(self):
        em.check_completeness(rows_written=0, expected_count=0, threshold=0.01)


class TestManifestAndChecksum:
    def test_sha256_matches_file(self, tmp_path):
        p = tmp_path / "f.bin"
        p.write_bytes(b"bedbase" * 1000)
        import hashlib
        expected = hashlib.sha256(p.read_bytes()).hexdigest()
        assert em.sha256_file(p) == expected

    def test_manifest_records_rows_written_not_count_query(self, tmp_path):
        """
        Regression: the count field must be rows actually written, not the value
        the count(*) query returned. Recording the live count was the PEPhub bug
        that made a short dump undetectable.
        """
        schema = pa.schema([("id", pa.string())])
        # The source yields only 3 rows even though a "count query" might say 10.
        rows = [("x",), ("y",), ("z",)]
        path = tmp_path / "bedbase_metadata_2026_01_01.parquet"
        fetch = lambda cur, lim: (rows if cur is None else [], "z")  # noqa: E731
        rows_written = em.export_parquet(path, schema, fetch, chunk_size=100)
        assert rows_written == 3

        entry = em.file_entry(path)
        entry["rows"] = rows_written
        entry["file_type"] = "metadata"

        manifest = em.write_manifest(
            tmp_path, "2026_01_01", [entry],
            started="s", ended="e", source_db="bedbase",
        )
        data = json.loads((tmp_path / "manifest_2026_01_01.json").read_text())
        assert data["files"][0]["rows"] == 3            # rows written
        assert data["files"][0]["rows"] != 10           # NOT the count-query value
        assert data["schema_version"] == em.SCHEMA_VERSION
        assert data["source_database"] == "bedbase"
        # Manifest checksum matches the file on disk.
        assert data["files"][0]["sha256"] == em.sha256_file(path)


class TestConfigInterpolation:
    def test_env_vars_interpolated(self, tmp_path, monkeypatch):
        monkeypatch.setenv("POSTGRES_HOST", "db.example.org")
        cfg = tmp_path / "config.yaml"
        cfg.write_text("database:\n  host: $POSTGRES_HOST\n  user: bob\n")
        loaded = em.load_config(str(cfg))
        assert loaded["database"]["host"] == "db.example.org"
        assert loaded["database"]["user"] == "bob"

    def test_password_with_yaml_metacharacters(self, tmp_path, monkeypatch):
        """
        Regression: a secret is expanded after YAML parsing, so a password
        containing YAML metacharacters (>, |, :, #) must not break the parser.
        Expanding the raw file text before parsing broke a real Postgres password.
        """
        secret = ">a|b:c#d{e}"
        monkeypatch.setenv("POSTGRES_PASSWORD", secret)
        cfg = tmp_path / "config.yaml"
        cfg.write_text("database:\n  host: h\n  password: $POSTGRES_PASSWORD\n")
        loaded = em.load_config(str(cfg))
        assert loaded["database"]["password"] == secret


class TestSchemaShape:
    def test_metadata_schema_starts_with_id(self):
        assert em.METADATA_SCHEMA.field(0).name == "id"

    def test_membership_is_two_columns(self):
        assert em.MEMBERSHIP_SCHEMA.names == ["bedset_id", "bedfile_id"]
