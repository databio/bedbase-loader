"""
Finish bbconf migration 845d978eac7d on production (Oct 2026).

bbconf 0.15.0 removed bed.pephub, but production was never migrated, so every
new bed insert failed with a NOT NULL violation. The migration's other changes
(two JSONB columns, analysis_files) are already there, and the database has no
alembic revision, so `alembic upgrade` cannot run as-is.

Without --apply this only reports. With --apply it drops bed.pephub and stamps
the database at bbconf's alembic head, after checking the rest of the
migration is already in place.

Usage: python fix_db_schema.py config.yaml [--apply]
"""

import os
import sys
from pathlib import Path

import bbconf
import yaml
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from bbconf.db_utils import Base
from sqlalchemy import URL, create_engine, inspect, text


def main(config_path: str, apply: bool) -> int:
    with open(config_path) as f:
        db = yaml.safe_load(os.path.expandvars(f.read()))["database"]
    url = URL.create(
        "postgresql+psycopg",
        username=db["user"],
        password=db["password"],
        host=db["host"],
        port=db["port"],
        database=db["database"],
    )
    engine = create_engine(url)
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(bbconf.__file__).parent / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url.render_as_string(hide_password=False))

    with engine.connect() as conn:
        insp = inspect(conn)
        revision = None
        if insp.has_table("alembic_version"):
            revision = conn.scalar(text("SELECT version_num FROM alembic_version"))
        cols = {t: {c["name"] for c in insp.get_columns(t)} for t in ("bed", "bed_stats", "bedsets")}
        state = {
            "alembic revision": revision,
            "bed.pephub": "pephub" in cols["bed"],
            "bed_stats.distributions": "distributions" in cols["bed_stats"],
            "bedsets.bedset_stats": "bedset_stats" in cols["bedsets"],
            "analysis_files table": insp.has_table("analysis_files"),
        }
        diffs = compare_metadata(MigrationContext.configure(conn), Base.metadata)

    for k, v in state.items():
        print(f"{k}: {v}")
    print(f"Differences between database and bbconf {bbconf.__version__} models ({len(diffs)}):")
    for d in diffs:
        print(f"  {d}")

    expected = {
        "alembic revision": None,
        "bed.pephub": True,
        "bed_stats.distributions": True,
        "bedsets.bedset_stats": True,
        "analysis_files table": True,
    }
    if state != expected:
        print("State is not what this fix expects. Not changing anything.", file=sys.stderr)
        return 1
    if not apply:
        print("Dry run. Re-run with --apply to drop bed.pephub and stamp head.")
        return 0

    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE bed DROP COLUMN pephub"))
    command.stamp(cfg, "head")
    print("Dropped bed.pephub and stamped the database at bbconf's alembic head.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], "--apply" in sys.argv))
