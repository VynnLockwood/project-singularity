"""Paged, sortable, filterable reads of one table in a managed schema for the grid editor (read-only role)."""
from __future__ import annotations

import csv
import datetime
import io
import json

import psycopg
from psycopg import sql
from psycopg.rows import tuple_row

from config.siconfig import get_config

from . import introspect, roles
from .conn import ro_connection
from .export import AsyncChunks, _Chunks
from .util import jsonable

MAX_PAGE = 200


class RowsError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _describe(table: str, schema: str = "master") -> dict:
    d = introspect.describe_table(table, schema)
    if d is None:
        raise RowsError("table not found", 404)
    return d


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _run(query: sql.Composable, params=()) -> list[dict]:
    limits = get_config()["limits"]
    try:
        with ro_connection() as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute(f"SET LOCAL statement_timeout = {int(limits['queryTimeoutMs'])}")
            return conn.execute(query, params).fetchall()
    except psycopg.Error as exc:
        raise RowsError(exc.diag.message_primary or "query failed") from exc


def _plan(cols: dict, role_cols: dict, *, sort, direction, filters, include_inactive):
    """The WHERE clause, ORDER BY and params for a filtered, sorted view. Shared by the rows endpoint and CSV export."""
    if direction not in ("asc", "desc"):
        raise RowsError("direction must be asc or desc")
    if sort is not None and sort not in cols:
        raise RowsError(f"unknown column {sort!r}")
    where, params = [], []
    for col, text in (filters or {}).items():
        if col not in cols:
            raise RowsError(f"unknown column {col!r}")
        if not isinstance(text, str):
            raise RowsError("filters must be text")
        if text:
            where.append(sql.SQL("{}::text ILIKE %s").format(sql.Identifier(col)))
            params.append(_like(text))
    active = role_cols.get("is_active")
    if active and not include_inactive:
        where.append(sql.SQL("{} IS DISTINCT FROM false").format(sql.Identifier(active)))
    clause = sql.SQL(" WHERE ") + sql.SQL(" AND ").join(where) if where else sql.SQL("")
    pk = role_cols.get("primary_key")
    order_col = sort or pk or next(iter(cols))
    order = sql.SQL("{} {}").format(sql.Identifier(order_col), sql.SQL(direction.upper()))
    if pk and order_col != pk:
        order += sql.SQL(", {}").format(sql.Identifier(pk))
    return clause, order, params


def read_rows(table: str, *, offset: int = 0, limit: int = 50, sort: str | None = None,
              direction: str = "asc", filters: dict | None = None, include_inactive: bool = False,
              schema: str = "master") -> dict:
    d = _describe(table, schema)
    cols = {c["name"]: c for c in d["columns"]}
    limit = max(1, min(int(limit), MAX_PAGE))
    offset = max(0, int(offset))
    conv = roles.conventions()
    role_cols = roles.roles_of(d, conv)
    hidden = roles.hidden_columns(conv)
    clause, order, params = _plan(cols, role_cols, sort=sort, direction=direction, filters=filters,
                                  include_inactive=include_inactive)
    pk = role_cols.get("primary_key")

    t = sql.Identifier(schema, table)
    total = _run(sql.SQL("SELECT count(*) AS n FROM {}{}").format(t, clause), params)[0]["n"]
    rows = _run(sql.SQL("SELECT * FROM {}{} ORDER BY {} LIMIT {} OFFSET {}").format(
        t, clause, order, sql.Literal(limit), sql.Literal(offset)), params)

    refs = {x["from"]["column"]: x["to"] for x in introspect.graph(schema)["relations"] if x["from"]["table"] == table}
    return {
        "table": table, "total": total, "offset": offset, "limit": limit, "primaryKey": pk,
        "columns": [{
            "name": c["name"], "type": c["type"], "nullable": c["nullable"],
            "readOnly": c["identity"] or c["name"] in hidden or c["name"] in role_cols.values(),
            "references": refs.get(c["name"]),
        } for c in d["columns"]],
        "rows": [{k: jsonable(v) for k, v in row.items()} for row in rows],
    }


def label_column(columns: dict, skip: set[str] | frozenset[str] = frozenset()) -> str | None:
    """The column that best names a row: *name, then *code, then the first other text column."""
    texts = [n for n, c in columns.items()
             if n not in skip and (c["type"] == "text" or c["type"].startswith("character varying"))]
    for suffix in ("name", "code"):
        for n in texts:
            if n.lower().endswith(suffix):
                return n
    return texts[0] if texts else None


def options(table: str, *, q: str = "", limit: int = 100, schema: str = "master") -> dict:
    """id + label pairs for a reference dropdown."""
    d = _describe(table, schema)
    cols = {c["name"]: c for c in d["columns"]}
    conv = roles.conventions()
    role_cols = roles.roles_of(d, conv)
    pk = role_cols.get("primary_key")
    if pk is None:
        raise RowsError("this table has no primary key column to reference")
    label = label_column(cols, set(role_cols.values()) | roles.hidden_columns(conv))
    limit = max(1, min(int(limit), MAX_PAGE))
    t = sql.Identifier(schema, table)
    label_expr = sql.Identifier(label) if label else sql.SQL("{}::text").format(sql.Identifier(pk))
    where, params = sql.SQL(""), []
    conds = []
    if q:
        conds.append(sql.SQL("{}::text ILIKE %s").format(label_expr))
        params.append(_like(q))
    active = role_cols.get("is_active")
    if active:
        conds.append(sql.SQL("{} IS DISTINCT FROM false").format(sql.Identifier(active)))
    if conds:
        where = sql.SQL(" WHERE ") + sql.SQL(" AND ").join(conds)
    rows = _run(sql.SQL("SELECT {} AS id, {} AS label FROM {}{} ORDER BY label LIMIT {}").format(
        sql.Identifier(pk), label_expr, t, where, sql.Literal(limit + 1)), params)
    return {
        "labelColumn": label, "truncated": len(rows) > limit,
        "options": [{"id": jsonable(r["id"]), "label": str(r["label"])} for r in rows[:limit]],
    }


_ORDERABLE = ("text", "character", "smallint", "integer", "bigint", "numeric", "real", "double precision",
              "date", "time", "timestamp")
TOP_VALUES = 10
VALUE_CUT = 80


def profile(table: str, column: str, *, schema: str = "master") -> dict:
    """What is in one column: counts, range for orderable types and the most common values.

    Table and column are checked against the real table first, then only ever used as identifiers.
    Counts only active rows when the table has an is_active role column.
    """
    d = _describe(table, schema)
    cols = {c["name"]: c for c in d["columns"]}
    if not isinstance(column, str) or column not in cols:
        raise RowsError(f"unknown column {column!r}")
    kind = cols[column]["type"].lower()
    role_cols = roles.roles_of(d, roles.conventions())
    t, col = sql.Identifier(schema, table), sql.Identifier(column)
    active = role_cols.get("is_active")
    where = sql.SQL(" WHERE {} IS DISTINCT FROM false").format(sql.Identifier(active)) if active else sql.SQL("")
    orderable = kind.startswith(_ORDERABLE) and not kind.endswith("]")
    distinct_of = sql.SQL("{}::text").format(col) if kind == "json" else col     # json has no equality operator
    head = _run(sql.SQL("SELECT count(*) AS total, count({c}) AS non_null, count(DISTINCT {dc}) AS distinct_count, "
                        "{lo} AS lo, {hi} AS hi FROM {t}{w}").format(
        c=col, dc=distinct_of, t=t, w=where,
        lo=sql.SQL("min({})").format(col) if orderable else sql.SQL("NULL"),
        hi=sql.SQL("max({})").format(col) if orderable else sql.SQL("NULL")))[0]
    present = sql.SQL("{} IS NOT NULL").format(col)
    top_where = (where + sql.SQL(" AND ") + present) if active else (sql.SQL(" WHERE ") + present)
    top = _run(sql.SQL("SELECT left(v, {cut}) AS value, count(*) AS n FROM (SELECT {c}::text AS v FROM {t}{w}) x "
                       "GROUP BY v ORDER BY count(*) DESC, v LIMIT {n}").format(
        cut=sql.Literal(VALUE_CUT), c=col, t=t, w=top_where, n=sql.Literal(TOP_VALUES)))
    return {
        "total": int(head["total"]), "null_count": int(head["total"]) - int(head["non_null"]),
        "distinct_count": int(head["distinct_count"]), "min": jsonable(head["lo"]), "max": jsonable(head["hi"]),
        "top_values": [{"value": r["value"], "count": int(r["n"])} for r in top],
    }


_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
CSV_BATCH = 1000


def csv_cell(value, type_name: str = "") -> str:
    """One CSV field, decided by the value (the column type only mattered for text, enums and jsonb alike).

    NULL is empty; any string a spreadsheet could run as a formula gets a leading apostrophe; booleans are
    `true`/`false` as in Postgres and the rows endpoint; jsonb objects and arrays are JSON text.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return "'" + value if value.startswith(_FORMULA_START) else value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    v = jsonable(value)
    return v if isinstance(v, str) else str(v)


class CsvExport:
    """A filtered, sorted view of one table as CSV chunks; same iterator contract as `export.Export`."""

    def __init__(self, table: str, schema: str, columns: list[dict], query: sql.Composable, params: list,
                 total: int, today: datetime.date | None = None):
        self.table, self.schema, self.columns = table, schema, columns
        self._query, self._params, self.total = query, params, total
        self.today = today or datetime.date.today()
        self.conn = None

    def filename(self) -> str:
        return f"{self.schema}-{self.table}-{self.today.isoformat()}.csv"

    def chunks(self) -> _Chunks:
        return _Chunks(self)

    def async_chunks(self) -> AsyncChunks:
        return AsyncChunks(self.chunks())

    def close(self) -> None:
        if self.conn is not None and not self.conn.closed:
            self.conn.close()

    def _generate(self):
        def encode(row) -> bytes:
            buf = io.StringIO(newline="")
            csv.writer(buf, lineterminator="\r\n").writerow(row)
            return buf.getvalue().encode("utf-8")

        types = [c["type"] for c in self.columns]
        yield b"\xef\xbb\xbf" + encode([c["name"] for c in self.columns])
        self.conn = ro_connection()
        self.conn.execute("SET TRANSACTION READ ONLY")
        self.conn.execute(f"SET LOCAL statement_timeout = {int(get_config()['limits']['queryTimeoutMs'])}")
        with self.conn.cursor(name="csv_export", row_factory=tuple_row) as cur:
            cur.execute(self._query, self._params)
            while batch := cur.fetchmany(CSV_BATCH):
                yield b"".join(encode([csv_cell(v, t) for v, t in zip(row, types)]) for row in batch)


def open_csv(table: str, *, sort: str | None = None, direction: str = "asc", filters: dict | None = None,
             include_inactive: bool = False, schema: str = "master", cap: int) -> CsvExport:
    d = _describe(table, schema)
    cols = {c["name"]: c for c in d["columns"]}
    role_cols = roles.roles_of(d, roles.conventions())
    clause, order, params = _plan(cols, role_cols, sort=sort, direction=direction, filters=filters,
                                  include_inactive=include_inactive)
    t = sql.Identifier(schema, table)
    total = _run(sql.SQL("SELECT count(*) AS n FROM {}{}").format(t, clause), params)[0]["n"]
    if total > cap:
        raise RowsError(f"{total} rows match; the most that can be exported is {cap} "
                        f"(setting limits.maxExportRows). Narrow the filters.")
    query = sql.SQL("SELECT * FROM {}{} ORDER BY {}").format(t, clause, order)
    return CsvExport(table, schema, d["columns"], query, params, total)
