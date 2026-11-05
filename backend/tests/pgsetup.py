"""Helpers that build scratch databases with the real restricted roles.

Needs a Postgres superuser: set TEST_PG_ADMIN_USER (and DB_HOST / DB_PORT, and
PGPASSWORD if the server asks for one). Roles are cluster-wide, so they are
created once and shared by every scratch database; the per-database part is
the real db/init/01-schemas-roles.sh with only its CREATE ROLE lines removed.
"""
import os
import re
import subprocess
from pathlib import Path

import psycopg

ADMIN = os.environ.get("TEST_PG_ADMIN_USER")
HOST = os.environ.get("DB_HOST", "localhost")
PORT = os.environ.get("DB_PORT", "5432")
REPO = Path(__file__).resolve().parents[2]
PASSWORDS = {
    "singularity_app": "app-pw",
    "singularity_master_rw": "rw-pw",
    "singularity_master_ro": "ro-pw",
}


def connect(user, dbname, autocommit=True, row_factory=None):
    kw = {"row_factory": row_factory} if row_factory else {}
    return psycopg.connect(
        host=HOST, port=PORT, user=user, dbname=dbname, autocommit=autocommit,
        password=PASSWORDS.get(user), **kw,
    )


def ensure_roles():
    with connect(ADMIN, "postgres") as admin:
        for role, pw in PASSWORDS.items():
            exists = admin.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
            verb = "ALTER" if exists else "CREATE"
            admin.execute(f"{verb} ROLE {role} LOGIN PASSWORD '{pw}'")


def create_scratch_db(name: str) -> None:
    ensure_roles()
    with connect(ADMIN, "postgres") as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {name}")
        have_vector = admin.execute("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'").fetchone()

    script = (REPO / "db/init/01-schemas-roles.sh").read_text()
    script = re.sub(r"^CREATE ROLE .*;\n", "", script, flags=re.MULTILINE)
    if not have_vector:
        stripped = script.replace("CREATE EXTENSION IF NOT EXISTS vector SCHEMA singularity;", "")
        assert stripped != script
        script = stripped
    scratch = Path(os.environ.get("TMPDIR", "/tmp")) / f"init-{name}.sh"
    scratch.write_text(script)
    env = {
        **os.environ, "PGHOST": HOST, "PGPORT": PORT, "POSTGRES_USER": ADMIN, "POSTGRES_DB": name,
        "DB_APP_PASSWORD": PASSWORDS["singularity_app"],
        "DB_MASTER_RW_PASSWORD": PASSWORDS["singularity_master_rw"],
        "DB_MASTER_RO_PASSWORD": PASSWORDS["singularity_master_ro"],
    }
    subprocess.run(["bash", str(scratch)], env=env, check=True)


def drop_db(name: str) -> None:
    with connect(ADMIN, "postgres") as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def wipe_master(dbname: str) -> None:
    """Drop every table in `master` (as its owner role) so each test starts empty."""
    with connect("singularity_master_rw", dbname) as rw:
        for (t,) in rw.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'master'").fetchall():
            rw.execute(f'DROP TABLE IF EXISTS master."{t}" CASCADE')
