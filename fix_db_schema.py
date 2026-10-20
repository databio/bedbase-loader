"""
Finish bbconf migration 845d978eac7d on production (Oct 2026).

bbconf 0.15.0 removed bed.pephub, but production was stamped at alembic head
without the column being dropped, so every new bed insert failed with a NOT
NULL violation.

Without --apply this only reports. With --apply it drops bed.pephub, after
checking that the database is at bbconf's alembic head and that bed.pephub is
the only difference from bbconf's models. Tables bbconf does not know about
(other projects share this database) are ignored.

Usage: python fix_db_schema.py config.yaml [--apply]
"""

import os
import sys
from pathlib import Path

import bbconf
import yaml
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from bbconf.db_utils import Base
from sqlalchemy import URL, create_engine, text


def main(config_path: str, apply: bool) -> int:
    with open(config_path) as f:
        db = {k: os.path.expandvars(str(v)) for k, v in yaml.safe_load(f)["database"].items()}
    url = URL.create(
        "postgresql+psycopg",
        username=db["user"],
        password=db["password"],
        host=db["host"],
        port=int(db["port"]),
        database=db["database"],
    )
    engine = create_engine(url)
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(bbconf.__file__).parent / "alembic"))
    head = ScriptDirectory.from_config(cfg).get_current_head()

    with engine.connect() as conn:
        revision = MigrationContext.configure(conn).get_current_revision()
        diffs = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    # remove_table = a table bbconf does not define, i.e. another project's.
    diffs = [d for d in diffs if not (isinstance(d, tuple) and d[0] == "remove_table")]

    print(f"Alembic revision: {revision} (bbconf {bbconf.__version__} head: {head})")
    print(f"Differences from bbconf models ({len(diffs)}):")
    for d in diffs:
        print(f"  {d}")

    pephub_only = (
        len(diffs) == 1
        and diffs[0][0] == "remove_column"
        and diffs[0][2] == "bed"
        and diffs[0][3].name == "pephub"
    )
    if revision == head and not diffs:
        print("Schema matches bbconf. Nothing to do.")
        return 0
    if revision != head or not pephub_only:
        print("State is not what this fix expects. Not changing anything.", file=sys.stderr)
        return 1
    if not apply:
        print("Dry run. Re-run with --apply to drop bed.pephub.")
        return 0

    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE bed DROP COLUMN pephub"))
    print("Dropped bed.pephub.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], "--apply" in sys.argv))
