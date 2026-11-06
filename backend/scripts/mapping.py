"""Mapping JSON: turn the tables of a parsed script into *our* tables, plus one bulk load per table.

A script (pg_dump style) usually has natural keys (`province.province_id = '10'`), while our tables
have a generated key `<table>_id` plus a unique business key. The mapping says how to translate:

    {"tables": {"<source_table>": {
        "target": "<target table>",                       # snake_case, <= 63 chars
        "columns": {"<target col>": "<source col>", ...},   # copied values (type/nullability from the script)
        "drop": ["<source col>", ...],                      # source columns deliberately not loaded
        "refs": [{"column": "<new FK column>",              # integer column -> parent's <parent target>_id
                  "parent": "<source table of the parent>",  # must be in the mapping too
                  "parent_key": "<parent target col>",       # unique column of the parent the value matches
                  "source_column": "<source col here>"}]}}}  # holds the parent's key; only feeds the join

Every source column must be mapped (to exactly one target column), dropped, or used as a ref's
source_column. Every table that the script creates or loads rows into must be in the mapping.

apply_mapping() returns gate ops (never executes SQL):
* tables the script creates: create_table <target> with `<target>_id` (integer identity primary key,
  not from the script), the mapped columns, and one integer column per ref; the script's PRIMARY KEY /
  UNIQUE constraints become keys on the mapped columns (the natural key becomes the business key):
  single-column ones as an inline `unique: true` on the column (constraint `<table>_<col>_key`, what the
  gate's business-key rule looks for), multi-column ones as add_unique;
  CHECKs (column names rewritten in the pglast AST), indexes, foreign keys and comments are carried over.
  CHECK constraints keep the script's name as-is (e.g. `chk_subdistrict_postcode`), even when the target
  table has another name; unique / index / foreign key names are left to the gate's defaults;
  each ref adds add_foreign_key (on_delete from the script's FK on that column, else "restrict");
* tables the script only loads rows into (no CREATE TABLE): only the bulk_insert, into an existing table;
* then one bulk_insert per table with rows, parents before children:
    {"op": "bulk_insert", "table": <target>, "columns": [<op col>, ...],
     "source": {"script_id": None, "stream": <source table>, "columns": [<stream col per op col>]},
     "refs": [{"column", "parent_table", "parent_key", "source_column": <op col holding the key>}],
     "expected_rows": <rows in the stream>}
  Op columns are the mapped target names, then one carrier column `_ref_<ref column>` per ref (the
  executor uses carriers only for the join). script_id is None here: bind_script() sets it once the
  script is stored (Task 9); a Change must never hold a bulk_insert with a null script_id.

User mistakes are returned as issues (ScriptIssue with line 0 and `statement` = the mapping path, or
the data line for bad COPY rows), never raised. When there are issues, `ops` is empty. Rows are read
(to count them and find bad COPY rows) only when the mapping itself has no issues, so bad rows show up
after the mapping is fixed. A ref whose source column is not in the script's row data is an issue.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field

import pglast
from pglast import ast
from pglast.stream import RawStream
from pglast.visitors import Visitor

from gate.operations import IDENT_RE, check_expression
from scripts.copyparse import RowStreamError, ScriptIssue
from scripts.sqlparse import ParsedScript

MAX_IDENT = 63
# same as the gate's snake_case naming style (gate.rules), anchored
SNAKE_RE = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")
DEFAULT_ON_DELETE = "restrict"
_TABLE_KEYS = {"target", "columns", "drop", "refs"}
_REF_KEYS = {"column", "parent", "parent_key", "source_column"}


@dataclass
class MappingResult:
    ops: list[dict] = field(default_factory=list)
    issues: list[ScriptIssue] = field(default_factory=list)


# ---------------------------------------------------------------- the script's tables

@dataclass
class _Src:
    name: str
    created: bool
    columns: dict[str, dict]                 # source column -> create_table column dict ({"name"} for stream-only)
    comment: str | None = None
    pk: str | None = None
    uniques: list[tuple[list[str], str]] = field(default_factory=list)   # (columns, label)
    indexes: list[dict] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)
    fks: list[dict] = field(default_factory=list)
    col_comments: dict[str, str] = field(default_factory=dict)

    def unique_cols(self) -> set[str]:
        """Columns that are unique on their own (single-column PK / UNIQUE / unique index)."""
        out = {self.pk} if self.pk else set()
        out |= {cols[0] for cols, _ in self.uniques if len(cols) == 1}
        out |= {i["columns"][0] for i in self.indexes if i.get("unique") and len(i["columns"]) == 1}
        return out

    def natural_key(self) -> str | None:
        if self.pk:
            return self.pk
        for cols, _ in self.uniques:
            if len(cols) == 1:
                return cols[0]
        return next((i["columns"][0] for i in self.indexes if i.get("unique") and len(i["columns"]) == 1), None)


def _sources(parsed: ParsedScript) -> tuple[dict[str, _Src], list[str]]:
    """Source tables in script order, and the names of tables that ops touch but the script neither
    creates nor loads (not supported with a mapping)."""
    srcs: dict[str, _Src] = {}
    for op in parsed.ops:
        if op["op"] == "create_table":
            cols = {c["name"]: c for c in op["columns"]}
            s = srcs[op["name"]] = _Src(op["name"], True, cols, comment=op.get("comment"))
            for c in op["columns"]:
                if c.get("primary_key"):
                    s.pk = c["name"]
                if c.get("unique"):
                    s.uniques.append(([c["name"]], f"UNIQUE on {c['name']}"))
                if c.get("comment") is not None:
                    s.col_comments[c["name"]] = c["comment"]
    for name, stream in parsed.streams.items():
        if name not in srcs:
            srcs[name] = _Src(name, False, {c: {"name": c} for c in stream.columns})
    foreign: list[str] = []
    for op in parsed.ops:
        kind = op["op"]
        if kind == "create_table":
            continue
        s = srcs.get(op["table"])
        if s is None or not s.created:
            if op["table"] not in foreign:
                foreign.append(op["table"])
            continue
        if kind == "add_unique":
            s.uniques.append((list(op["columns"]), f"unique constraint {op.get('name') or ', '.join(op['columns'])}"))
        elif kind == "add_index":
            s.indexes.append(op)
        elif kind == "add_check":
            s.checks.append(op)
        elif kind == "add_foreign_key":
            s.fks.append(op)
        elif kind == "set_table_comment":
            s.comment = op["comment"]
        elif kind == "set_column_comment":
            s.col_comments[op["column"]] = op["comment"]
    return srcs, foreign


# ---------------------------------------------------------------- helpers

def _name_ok(name: str) -> bool:
    return (isinstance(name, str) and len(name) <= MAX_IDENT and SNAKE_RE.fullmatch(name) is not None
            and IDENT_RE.fullmatch(name) is not None)


def _snake(name: str, limit: int = MAX_IDENT, prefix: str = "c") -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    if not s or not s[0].isalpha():
        s = f"{prefix}_{s}".rstrip("_")
    return s[:limit].rstrip("_")


def _unique_name(base: str, used: set[str], limit: int = MAX_IDENT) -> str:
    name, n = base, 2
    while name in used:
        suffix = f"_{n}"
        name = base[: limit - len(suffix)].rstrip("_") + suffix
        n += 1
    used.add(name)
    return name


class _Renamer(Visitor):
    def __init__(self, names: dict[str, str]):
        super().__init__()
        self.names = names
        self.missing: list[str] = []

    def visit_ColumnRef(self, ancestors, node):  # noqa: N802 - pglast visitor naming
        f = node.fields
        if len(f) == 1 and isinstance(f[0], ast.String):
            new = self.names.get(f[0].sval)
            if new is None:
                self.missing.append(f[0].sval)
            else:
                node.fields = (ast.String(sval=new),)


def _rewrite_check(expr: str, names: dict[str, str]) -> tuple[str | None, list[str]]:
    """Expression with column references renamed (source -> target); (None, missing) if some are not kept."""
    try:
        val = pglast.parse_sql(f"SELECT ({expr})")[0].stmt.targetList[0].val
    except Exception:  # noqa: BLE001 - parser output is re-parsed; failure is reported as an issue
        return None, []
    r = _Renamer(names)
    r(val)
    if r.missing:
        return None, sorted(set(r.missing))
    return RawStream()(val), []


def _topo(nodes: list[str], deps: dict[str, set[str]]) -> tuple[list[str], list[str]]:
    """(order parents-first, nodes left in a cycle)."""
    done: list[str] = []
    pending = list(nodes)
    while pending:
        nxt = next((t for t in pending if deps[t] <= set(done)), None)
        if nxt is None:
            return done, pending
        done.append(nxt)
        pending.remove(nxt)
    return done, []


# ---------------------------------------------------------------- shape of the mapping

def _shape(mapping) -> list[ScriptIssue]:
    out: list[ScriptIssue] = []

    def bad(path, msg):
        out.append(ScriptIssue(0, path, msg))

    if not isinstance(mapping, dict) or set(mapping) != {"tables"} or not isinstance(mapping["tables"], dict):
        bad("mapping", 'mapping must be {"tables": {"<source table>": {...}}}')
        return out
    for src, m in mapping["tables"].items():
        p = f"tables.{src}"
        if not isinstance(m, dict):
            bad(p, "must be an object {target, columns, drop?, refs?}")
            continue
        if set(m) - _TABLE_KEYS:
            bad(p, f"unknown keys {sorted(set(m) - _TABLE_KEYS)} (allowed: {sorted(_TABLE_KEYS)})")
        if not isinstance(m.get("target"), str):
            bad(f"{p}.target", "target must be the target table name (text)")
        cols = m.get("columns")
        if not isinstance(cols, dict) or not all(isinstance(v, str) for v in cols.values()):
            bad(f"{p}.columns", 'columns must be {"<target column>": "<source column>"}')
        drop = m.get("drop", [])
        if not isinstance(drop, list) or not all(isinstance(d, str) for d in drop):
            bad(f"{p}.drop", "drop must be a list of source column names")
        refs = m.get("refs", [])
        if not isinstance(refs, list):
            bad(f"{p}.refs", "refs must be a list")
            continue
        for i, r in enumerate(refs):
            if not isinstance(r, dict) or set(r) != _REF_KEYS or not all(isinstance(v, str) for v in r.values()):
                bad(f"{p}.refs[{i}]", "each ref must be {column, parent, parent_key, source_column} (text values)")
    return out


# ---------------------------------------------------------------- apply

def apply_mapping(parsed: ParsedScript, mapping: dict) -> MappingResult:
    """Translate the script's tables through `mapping` into gate ops (see module docstring)."""
    issues = _shape(mapping)
    if issues:
        return MappingResult([], issues)
    srcs, foreign = _sources(parsed)
    tables: dict[str, dict] = mapping["tables"]

    def bad(path, msg):
        issues.append(ScriptIssue(0, path, msg))

    for t in foreign:
        bad(f"tables.{t}", f"the script changes table {t!r}, which it neither creates nor loads rows into; "
                           "a mapped script may only change tables it creates")
    for name in srcs:
        if name not in tables:
            what = "loads rows into" if name in parsed.streams else "creates"
            bad(f"tables.{name}", f"the script {what} table {name!r} but the mapping has no entry for it")
    for name in tables:
        if name not in srcs:
            bad(f"tables.{name}", f"source table {name!r} does not exist in the script "
                                  f"(script tables: {', '.join(srcs) or 'none'})")
    mapped = [n for n in srcs if n in tables]  # script order

    # --- names
    target_of = {n: tables[n]["target"] for n in mapped}
    seen_targets: dict[str, str] = {}
    for n in mapped:
        p, tgt = f"tables.{n}", target_of[n]
        if not _name_ok(tgt):
            bad(f"{p}.target", f"target table name {tgt!r} must be snake_case (lowercase letters, digits, "
                               f"single underscores, starting with a letter) and at most {MAX_IDENT} characters")
        elif srcs[n].created and len(f"{tgt}_id") > MAX_IDENT:
            bad(f"{p}.target", f"target table name {tgt!r} is too long: its key column {tgt}_id would be longer "
                               f"than {MAX_IDENT} characters")
        if tgt in seen_targets:
            bad(f"{p}.target", f"target table {tgt!r} is used by more than one source table "
                               f"({seen_targets[tgt]!r} and {n!r})")
        seen_targets.setdefault(tgt, n)

    # --- columns, drop, refs
    plain: dict[str, dict[str, str]] = {}      # source table -> {source col -> target col}
    ref_src: dict[str, dict[str, str]] = {}    # source table -> {source col -> ref column}
    for n in mapped:
        s, m, p = srcs[n], tables[n], f"tables.{n}"
        tgt = target_of[n]
        surrogate = f"{tgt}_id" if s.created else None
        used: set[str] = set()
        plain[n], ref_src[n] = {}, {}
        for tc, sc in m["columns"].items():
            if not _name_ok(tc):
                bad(f"{p}.columns.{tc}", f"target column name {tc!r} must be snake_case and at most {MAX_IDENT} characters")
            if tc == surrogate:
                bad(f"{p}.columns.{tc}", f"column name {tc!r} is reserved for the generated key of {tgt!r}")
            if sc not in s.columns:
                bad(f"{p}.columns.{tc}", f"source column {n}.{sc} does not exist "
                                         f"(columns: {', '.join(s.columns)})")
            elif sc in plain[n]:
                bad(f"{p}.columns.{tc}", f"source column {n}.{sc} is mapped to more than one target column "
                                         f"({plain[n][sc]!r} and {tc!r}); map it once")
            else:
                plain[n][sc] = tc
            used.add(tc)
        for d in m.get("drop", []):
            if d not in s.columns:
                bad(f"{p}.drop", f"dropped column {n}.{d} does not exist")
            elif d in plain[n]:
                bad(f"{p}.drop", f"column {n}.{d} is both mapped and dropped")
        for i, r in enumerate(m.get("refs", [])):
            rp = f"{p}.refs[{i}]"
            rc, parent, pkey, sc = r["column"], r["parent"], r["parent_key"], r["source_column"]
            if not _name_ok(rc):
                bad(rp, f"ref column name {rc!r} must be snake_case and at most {MAX_IDENT} characters")
            if rc == surrogate:
                bad(rp, f"column name {rc!r} is reserved for the generated key of {tgt!r}")
            elif rc in used:
                bad(rp, f"column {rc!r} appears more than once in {tgt!r}")
            used.add(rc)
            if sc not in s.columns:
                bad(rp, f"source column {n}.{sc} does not exist (columns: {', '.join(s.columns)})")
            else:
                ref_src[n].setdefault(sc, rc)
            if parent == n:
                bad(rp, f"a table cannot refer to itself in a load ({n!r}); keep the column as a plain value")
                continue
            if parent not in tables or parent not in srcs:
                bad(rp, f"parent {parent!r} is not a mapped table of the script")
                continue
            ps, pm = srcs[parent], tables[parent]
            if s.created and not ps.created:
                bad(rp, f"parent {parent!r} is not created by the script; a created table can only refer "
                        "to tables created in the same script")
            pcols = pm.get("columns") if isinstance(pm.get("columns"), dict) else {}
            if pkey not in pcols:
                bad(rp, f"parent_key {pkey!r} is not a column of the parent mapping {parent!r} "
                        f"(columns: {', '.join(pcols)})")
            elif ps.created and pcols[pkey] not in ps.unique_cols():
                bad(rp, f"parent_key {pkey!r} ({parent}.{pcols[pkey]}) must be unique in the parent "
                        "(its primary key or a unique column)")
            fk = next((f for f in s.fks if f["column"] == sc), None)
            if fk is not None:
                if fk["ref_table"] != parent:
                    bad(rp, f"{n}.{sc} references {fk['ref_table']} in the script, not {parent}")
                elif pkey in pcols:
                    target_col = fk.get("ref_column") or ps.pk
                    if target_col != pcols[pkey]:
                        bad(rp, f"{n}.{sc} references {parent}.{target_col} in the script, but parent_key "
                                f"{pkey!r} is {parent}.{pcols[pkey]}")
        dropped = set(m.get("drop", []))
        for c in s.columns:
            if c not in plain[n] and c not in ref_src[n] and c not in dropped:
                bad(p, f"source column {n}.{c} is not mapped; map it in columns, use it in refs, or list it in drop")

    # --- schema ops for created tables
    creates: dict[str, dict] = {}
    uniques: list[dict] = []
    unique_idx: list[dict] = []
    checks: list[dict] = []
    fks: list[dict] = []
    plain_idx: list[dict] = []
    deps: dict[str, set[str]] = {n: set() for n in mapped}

    def kept(n: str, c: str) -> str | None:
        return plain[n].get(c) or ref_src[n].get(c)

    for n in mapped:
        s, m, p, tgt = srcs[n], tables[n], f"tables.{n}", target_of[n]
        for r in m.get("refs", []):
            if r["parent"] in tables and r["parent"] != n:
                deps[n].add(r["parent"])
        if not s.created:
            continue
        cols: list[dict] = [{"name": f"{tgt}_id", "type": "integer", "primary_key": True, "identity": True,
                             "nullable": False}]
        for tc, sc in m["columns"].items():
            if sc not in s.columns:
                continue
            c = {k: v for k, v in s.columns[sc].items()
                 if k not in ("name", "primary_key", "identity", "references", "comment", "unique")}
            c = {"name": tc, **copy.deepcopy(c)}
            if sc == s.pk:
                c["nullable"] = False
            if sc in s.col_comments:
                c["comment"] = s.col_comments[sc]
            cols.append(c)
        for r in m.get("refs", []):
            sc = r["source_column"]
            if sc not in s.columns:
                continue
            src_col = s.columns[sc]
            c = {"name": r["column"], "type": "integer",
                 "nullable": bool(src_col.get("nullable", True)) and sc != s.pk}
            if sc in s.col_comments and plain[n].get(sc) is None:
                c["comment"] = s.col_comments[sc]
            cols.append(c)
            if r["parent"] in tables and r["parent"] in srcs and r["parent"] != n:
                fk = next((f for f in s.fks if f["column"] == sc), None)
                fks.append({"op": "add_foreign_key", "table": tgt, "column": r["column"],
                            "ref_table": target_of[r["parent"]], "ref_column": f"{target_of[r['parent']]}_id",
                            "on_delete": fk["on_delete"] if fk else DEFAULT_ON_DELETE})
        create = {"op": "create_table", "name": tgt, "columns": cols}
        if s.comment is not None:
            create["comment"] = s.comment
        creates[n] = create

        def make_unique(col_name: str, cols=cols) -> None:
            # single-column key: inline UNIQUE (constraint <table>_<col>_key), which the gate's
            # business-key rule recognises; multi-column keys stay add_unique
            next(c for c in cols if c["name"] == col_name)["unique"] = True

        if s.pk:
            k = kept(n, s.pk)
            if k is None:
                bad(p, f"primary key column {n}.{s.pk} is dropped; map it (it becomes the unique business key)")
            else:
                make_unique(k)
        for cols_, label in s.uniques:
            ks = [kept(n, c) for c in cols_]
            if None in ks:
                bad(p, f"{label} of {n} uses a dropped column "
                       f"({', '.join(c for c, k in zip(cols_, ks) if k is None)})")
            elif len(ks) == 1:
                make_unique(ks[0])
            else:
                uniques.append({"op": "add_unique", "table": tgt, "columns": ks})
        for ix in s.indexes:
            ks = [kept(n, c) for c in ix["columns"]]
            if None in ks:
                bad(p, f"index {ix.get('name')} of {n} uses a dropped column "
                       f"({', '.join(c for c, k in zip(ix['columns'], ks) if k is None)})")
                continue
            op = {"op": "add_index", "table": tgt, "columns": ks}
            if ix.get("unique"):
                op["unique"] = True
            (unique_idx if ix.get("unique") else plain_idx).append(op)
        for ck in s.checks:
            expr, missing = _rewrite_check(ck["expression"], plain[n])
            if expr is None:
                bad(p, f"check {ck['name']} of {n} uses column(s) the mapping does not keep as values "
                       f"({', '.join(missing) or '?'}); map them in columns or drop the check from the script")
                continue
            problems = check_expression(expr, {c["name"] for c in cols})
            if problems:
                bad(p, f"check {ck['name']} of {n} after renaming: {'; '.join(problems)}")
                continue
            checks.append({"op": "add_check", "table": tgt, "name": ck["name"], "expression": expr})
        ref_cols = {r["source_column"]: r for r in m.get("refs", [])}
        for fk in s.fks:
            c = fk["column"]
            r = ref_cols.get(c)
            if r is not None:
                continue  # carried by the ref (a ref to another parent is reported above)
            label = f"foreign key {fk.get('name') or c} of {n}"
            if c not in plain[n]:
                bad(p, f"{label} uses column {c}, which the mapping does not keep as a value")
                continue
            rt = fk["ref_table"]
            if rt not in srcs:  # a table outside the script (already in master): unchanged
                fks.append({"op": "add_foreign_key", "table": tgt, "column": plain[n][c], "ref_table": rt,
                            "ref_column": fk.get("ref_column"), "on_delete": fk["on_delete"]})
                continue
            if rt not in tables:
                continue  # reported above (no mapping entry)
            rc = fk.get("ref_column") or srcs[rt].pk
            prc = plain[rt].get(rc) if rt in plain else None
            if prc is None:
                bad(p, f"{label} references {rt}.{rc}, which the mapping of {rt!r} does not keep as a value")
                continue
            fks.append({"op": "add_foreign_key", "table": tgt, "column": plain[n][c],
                        "ref_table": target_of[rt], "ref_column": prc, "on_delete": fk["on_delete"]})
            if rt != n:
                deps[n].add(rt)

    order, cycle = _topo(mapped, deps)
    if cycle:
        bad("tables", f"the tables refer to each other in a cycle ({', '.join(cycle)}); "
                      "rows cannot be loaded parents first")

    # --- bulk loads (rows are counted only for a mapping without issues: counting reads every row)
    if issues:
        return MappingResult([], issues)
    bulks: list[dict] = []
    for n in order:
        stream = parsed.streams.get(n)
        if stream is None:
            continue
        count = 0
        try:
            for _ in stream.rows():
                count += 1
        except RowStreamError as exc:
            issues.append(exc.issue)
            continue
        if count == 0:
            continue
        m, tgt = tables[n], target_of[n]
        op_cols: list[str] = []
        src_cols: list[str] = []
        for tc, sc in m["columns"].items():
            if sc in stream.columns:
                op_cols.append(tc)
                src_cols.append(sc)
        refs: list[dict] = []
        used = set(op_cols)
        for i, r in enumerate(m.get("refs", [])):
            if r["source_column"] not in stream.columns:
                bad(f"tables.{n}.refs[{i}]", f"ref {r['column']!r} of {n!r} reads source column "
                                             f"{r['source_column']!r}, which has no data in the script "
                                             f"(the rows of {n} have columns: {', '.join(stream.columns)})")
                continue
            carrier = f"_ref_{r['column']}"[:MAX_IDENT]
            if carrier in used:
                carrier = _unique_name(f"_ref{i}", used)
            used.add(carrier)
            op_cols.append(carrier)
            src_cols.append(r["source_column"])
            refs.append({"column": r["column"], "parent_table": target_of[r["parent"]],
                         "parent_key": r["parent_key"], "source_column": carrier})
        if not op_cols:
            bad(f"tables.{n}", f"none of the mapped columns of {n!r} has data in the script")
            continue
        bulks.append({"op": "bulk_insert", "table": tgt, "columns": op_cols,
                      "source": {"script_id": None, "stream": n, "columns": src_cols},
                      "refs": refs, "expected_rows": count})

    if issues:
        return MappingResult([], issues)
    seen_u: set[tuple] = set()
    deduped: list[dict] = []
    for u in uniques:  # a PK and a UNIQUE on the same column(s) give one business key
        key = (u["table"], tuple(u["columns"]))
        if key not in seen_u:
            seen_u.add(key)
            deduped.append(u)
    ops = ([creates[n] for n in order if n in creates] + deduped + unique_idx + checks + fks + plain_idx + bulks)
    return MappingResult(ops, [])


def bind_script(ops: list[dict], script_id: int) -> list[dict]:
    """Copy of `ops` with every bulk_insert bound to the stored script `script_id`."""
    out = copy.deepcopy(ops)
    for o in out:
        if o.get("op") == "bulk_insert":
            o["source"]["script_id"] = script_id
    return out


# ---------------------------------------------------------------- suggest

def suggest_mapping(parsed: ParsedScript) -> dict:
    """A valid starting mapping: snake_case names, natural key -> `code` when it is named
    `<table>_id` / `<table>_code`, and a ref for each single-column FK to a parent's natural key."""
    srcs, _ = _sources(parsed)
    used_tables: set[str] = set()
    target = {n: _unique_name(_snake(n, MAX_IDENT - 3, "t"), used_tables, MAX_IDENT - 3) for n in srcs}

    # refs first (they decide which columns are not copied)
    ref_of: dict[str, list[tuple[str, str]]] = {n: [] for n in srcs}   # (source fk column, parent)
    for n, s in srcs.items():
        for fk in s.fks:
            parent = fk["ref_table"]
            ps = srcs.get(parent)
            if ps is None or parent == n or not ps.created or fk["column"] == s.pk:
                continue
            pk = ps.natural_key()
            if pk is None or (fk.get("ref_column") or ps.pk) != pk or any(f["column"] == pk for f in ps.fks):
                continue
            if any(c == fk["column"] for c, _ in ref_of[n]):
                continue
            ref_of[n].append((fk["column"], parent))

    names: dict[str, dict[str, str]] = {}   # source table -> {source col -> target col}
    used_of: dict[str, set[str]] = {}
    for n, s in srcs.items():
        tgt = target[n]
        used = used_of[n] = {f"{tgt}_id"} if s.created else set()
        key = s.natural_key() if s.created else None
        ref_cols = {c for c, _ in ref_of[n]}
        names[n] = {}
        for c in s.columns:
            if c in ref_cols:
                continue
            base = _snake(c)
            if c == key and base in (f"{tgt}_id", f"{tgt}_code", f"{_snake(n)}_id", f"{_snake(n)}_code"):
                base = "code"
            names[n][c] = _unique_name(base, used)

    out: dict[str, dict] = {}
    for n, s in srcs.items():
        used = used_of[n]
        refs = []
        for c, parent in ref_of[n]:
            pkey = names[parent][srcs[parent].natural_key()]
            refs.append({"column": _unique_name(f"{target[parent]}_id", used), "parent": parent,
                         "parent_key": pkey, "source_column": c})
        out[n] = {"target": target[n], "columns": {tc: sc for sc, tc in names[n].items()}, "drop": [], "refs": refs}
    return {"tables": out}
