"""Connections to the `master` schema as the two restricted DB roles."""
from __future__ import annotations

import psycopg
from django.conf import settings
from psycopg.rows import dict_row


def _connect(role: str, **kw) -> psycopg.Connection:
    c = settings.MASTER_DB
    return psycopg.connect(
        host=c["host"], port=c["port"], dbname=c["dbname"],
        user=c[f"{role}_user"], password=c[f"{role}_password"] or None,
        row_factory=dict_row, **kw,
    )


def ro_connection(**kw) -> psycopg.Connection:
    """Read-only role. Used for introspection, queries and pg_dump."""
    return _connect("ro", **kw)


def rw_connection(**kw) -> psycopg.Connection:
    """The only connection that can change `master`. Used by the apply step alone."""
    return _connect("rw", **kw)
