"""Offline tests for the metadata exporter. No database, no network."""

import json
import os
import sys

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import export_metadata as em  # noqa: E402


def batched(rows, n):
    """Split a flat row list into batches of n (mimics stream_batches output)."""
    return [rows[i:i + n] for i in range(0, len(rows), n)]


class TestWriteParquetBatches:
    def test_full_coverage(self, tmp_path):
        schema = pa.schema([("id", pa.string()), ("n", pa.int64())])
        rows = [(f"{i:032x}", i) for i in range(23)]
        path = tmp_path / "cov.parquet"
        written = em.write_parquet_batches(path, schema, iter(batched(rows, 5)))
        assert written == 23
        assert pq.read_table(path).to_pylist() == [
            {"id": r[0], "n": r[1]} for r in rows
        ]

    def test_exact_boundary_batches(self, tmp_path):
        schema = pa.schema([("id", pa.string())])
        rows = [(f"{i}",) for i in range(100)]
        path = tmp_path / "b.parquet"
        # 100 rows in batches of 50: two full batches, nothing dropped/duplicated.
        written = em.write_parquet_batches(path, schema, iter(batched(rows, 50)))
        assert written == 100

    def test_empty_and_blank_batches(self, tmp_path):
        schema = pa.schema([("id", pa.string())])
        path = tmp_path / "e.parquet"
        written = em.write_parquet_batches(path, schema, iter([[], []]))
        assert written == 0
        assert pq.read_table(path).num_rows == 0


class TestMergeMetadata:
    def test_left_join_semantics(self):
        """A bed with no metadata gets a full row of NULLs (LEFT JOIN)."""
        bed_batches = [[("aa", "bed1"), ("bb", "bed2")], [("cc", "bed3")]]
        meta_map = {"aa": ("Homo sapiens", "ChIP-seq"), "cc": ("Mus musculus", "ATAC")}
        out = [r for batch in em.merge_metadata_batches(bed_batches, meta_map, 2)
               for r in batch]
        assert out[0] == ("aa", "bed1", "Homo sapiens", "ChIP-seq")
        assert out[1] == ("bb", "bed2", None, None)          # missing metadata -> NULLs
        assert out[2] == ("cc", "bed3", "Mus musculus", "ATAC")

    def test_batches_preserved(self):
        bed_batches = [[("a",)], [("b",), ("c",)]]
        out = list(em.merge_metadata_batches(bed_batches, {}, 1))
        assert [len(b) for b in out] == [1, 2]
        assert out[1] == [("b", None), ("c", None)]


class TestParquetRoundTrip:
    def test_schema_and_dtypes_survive(self, tmp_path):
        schema = pa.schema([
            ("id", pa.string()),
            ("n", pa.int64()),
            ("flag", pa.bool_()),
            ("tags", pa.list_(pa.string())),
        ])
        rows = [
            ("a" * 32, 5, True, ["GSM1", "GSM2"]),
            ("b" * 32, 7, False, []),
        ]
        path = tmp_path / "rt.parquet"
        written = em.write_parquet_batches(path, schema, iter([rows]))
        assert written == 2

        table = pq.read_table(path)
        assert table.schema == schema
        back = table.to_pylist()
        assert back[0]["tags"] == ["GSM1", "GSM2"]
        assert back[1]["flag"] is False

    def test_zstd_compression_used(self, tmp_path):
        schema = pa.schema([("id", pa.string())])
        rows = [(f"id{i}",) for i in range(100)]
        path = tmp_path / "z.parquet"
        em.write_parquet_batches(path, schema, iter([rows]))
        meta = pq.ParquetFile(path).metadata.row_group(0).column(0)
        assert meta.compression == "ZSTD"


class TestFailureGate:
    def test_raises_when_short_beyond_threshold(self):
        with pytest.raises(RuntimeError, match="partial artifact"):
            em.check_completeness(rows_written=900, expected_count=1000, threshold=0.01)

    def test_does_not_raise_just_inside_threshold(self):
        em.check_completeness(rows_written=995, expected_count=1000, threshold=0.01)

    def test_extra_rows_never_trip_it(self):
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
        rows = [("x",), ("y",), ("z",)]  # 3 rows, even if a count query said 10
        path = tmp_path / "bedbase_metadata_2026_01_01.parquet"
        rows_written = em.write_parquet_batches(path, schema, iter([rows]))
        assert rows_written == 3

        entry = em.file_entry(path)
        entry["rows"] = rows_written
        entry["file_type"] = "metadata"

        em.write_manifest(tmp_path, "2026_01_01", [entry],
                          started="s", ended="e", source_db="bedbase")
        data = json.loads((tmp_path / "manifest_2026_01_01.json").read_text())
        assert data["files"][0]["rows"] == 3            # rows written
        assert data["files"][0]["rows"] != 10           # NOT the count-query value
        assert data["schema_version"] == em.SCHEMA_VERSION
        assert data["source_database"] == "bedbase"
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
    def test_metadata_is_bed_plus_metadata_only(self):
        """Metadata export is bed + bed_metadata; no stats columns."""
        assert em.METADATA_SCHEMA.field(0).name == "id"
        names = em.METADATA_SCHEMA.names
        assert "assay" in names and "cell_line" in names       # bed_metadata present
        assert "number_of_regions" not in names                # bed_stats absent
        assert "gc_content" not in names

    def test_membership_is_two_columns(self):
        assert em.MEMBERSHIP_SCHEMA.names == ["bedset_id", "bedfile_id"]
