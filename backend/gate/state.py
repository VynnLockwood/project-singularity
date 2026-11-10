"""Simulated schema state: lets the gate validate a whole operation list *before*
anything runs, including operations that depend on earlier ones in the same list.

State shape (also produced by schema.introspect.snapshot):
  {table: {"columns": {name: {"type": str, "nullable": bool}},
           "indexes": {name, ...}, "constraints": {name, ...}}}
"""
from __future__ import annotations

import copy
from typing import Iterator

from .conventions import Conventions
from .operations import check_expression, normalize_type, view_references


class State(dict):
    """The tables of one schema ({table: {...}}) plus its views and enum types.

    A dict subclass so every loop over tables keeps working; views and enums ride along as attributes.
    views: {name: {"query": str, "tables": set[str]}}; enums: {name: [value, ...]} in sort order.
    """

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.views: dict[str, dict] = {}
        self.enums: dict[str, list[str]] = {}


def views_of(state: dict) -> dict:
    return getattr(state, "views", {})


def enums_of(state: dict) -> dict:
    return getattr(state, "enums", {})


def _trim(s: str) -> str:
    return s[:63]


def index_name(table: str, cols: list[str]) -> str:
    return _trim(f"{table}_{'_'.join(cols)}_idx")


def unique_name(table: str, cols: list[str]) -> str:
    return _trim(f"uq_{table}_{'_'.join(cols)}")


def fk_name(table: str, col: str) -> str:
    return _trim(f"fk_{table}_{col}")


def has_column(state: dict, table: str, column: str) -> bool:
    return column in state.get(table, {}).get("columns", {})


def pk_of(state: dict, table: str) -> str | None:
    """Primary key column of `table`: the database's own, else the one the rules call primary_key."""
    t = state.get(table)
    return (t.get("pk") or t.get("roles", {}).get("primary_key")) if t else None


def role_of(state: dict, table: str, role: str) -> str | None:
    """Column of `table` that plays `role` (updated_at, is_active, ...), as set by the rules."""
    t = state.get(table)
    return t.get("roles", {}).get(role) if t else None


def refresh_roles(state: dict, table: str, conv: Conventions | None) -> None:
    if conv is not None and table in state:
        state[table]["roles"] = conv.table_roles(table, state[table]["columns"], state[table].get("pk"))


def with_roles(state: dict, conv: Conventions) -> dict:
    """Copy of `state` in which every table knows which of its columns play which role."""
    st = copy.deepcopy(state)
    for t in st:
        refresh_roles(st, t, conv)
    return st


def ref_column(state: dict, ref: dict, conv: Conventions | None = None, own_table: str | None = None,
               own_cols=None) -> str | None:
    """Column a reference points at: the one given, else the target table's primary key."""
    if ref.get("column"):
        return ref["column"]
    if own_table is not None and ref["table"] == own_table and own_cols is not None:
        explicit = next((c["name"] for c in own_cols if c.get("primary_key")), None)
        names = {c["name"] for c in own_cols}
        return conv.resolve("primary_key", own_table, names, explicit) if conv else explicit
    return pk_of(state, ref["table"])


def _col_entry(col: dict) -> dict:
    nullable = col.get("nullable", True) and not col.get("primary_key", False)
    return {"type": normalize_type(col["type"]), "nullable": nullable, "default": col.get("default")}


def _add_key(t: dict, name: str, cols) -> None:
    """Record a primary key or unique column set (under its constraint or index name)."""
    key = frozenset(cols)
    t.setdefault("keys", set()).add(key)
    t.setdefault("key_names", {})[name] = key


def _drop_key_named(t: dict, name: str) -> None:
    key = t.get("key_names", {}).pop(name, None)
    if key is not None and key not in t.get("key_names", {}).values():
        t.get("keys", set()).discard(key)


def _link(state: dict, a: str, b: str) -> None:
    if a in state and b in state and a != b:
        state[a].setdefault("links", set()).add(b)
        state[b].setdefault("links", set()).add(a)


def _unlink_all(state: dict, name: str) -> None:
    for other in state.get(name, {}).get("links", set()):
        if other in state:
            state[other].get("links", set()).discard(name)


def apply_op(op: dict, state: dict, errors: list[str] | None = None, label: str = "",
             conv: Conventions | None = None) -> None:
    """Apply one operation to `state` in place; append problems to `errors` if given."""
    err = (lambda m: errors.append(f"{label}{m}")) if errors is not None else (lambda m: None)
    kind = op["op"]

    def need_table(t):
        if t not in state:
            err(f"table {t!r} does not exist")
            return False
        return True

    def need_col(t, c):
        if t in state and c not in state[t]["columns"]:
            err(f"column {t}.{c} does not exist")
            return False
        return t in state

    if kind == "create_table":
        name = op["name"]
        if name in state:
            err(f"table {name!r} already exists")
        cols = {}
        constraints = set()
        pk = None
        new: dict = {}
        for c in op["columns"]:
            cols[c["name"]] = _col_entry(c)
            if c.get("primary_key"):
                pk = c["name"]
                _add_key(new, _trim(f"{name}_pkey"), [c["name"]])
            if c.get("unique"):
                constraints.add(_trim(f"{name}_{c['name']}_key"))
                _add_key(new, _trim(f"{name}_{c['name']}_key"), [c["name"]])
            if c.get("primary_key"):
                constraints.add(_trim(f"{name}_pkey"))
            ref = c.get("references")
            if ref:
                rt, rc = ref["table"], ref_column(state, ref, conv, name, op["columns"])
                if rt != name and rt not in state:
                    err(f"column {c['name']} references missing table {rt!r}")
                elif rt != name and rc not in state[rt]["columns"]:
                    err(f"column {c['name']} references missing column {rt}.{rc}")
                elif rt == name and rc not in {x["name"] for x in op["columns"]}:
                    err(f"column {c['name']} references missing column {rt}.{rc}")
                constraints.add(fk_name(name, c["name"]))
        state[name] = {"columns": cols, "indexes": set(), "constraints": constraints, "pk": pk,
                       "comment": op.get("comment"), "keys": new.get("keys", set()),
                       "key_names": new.get("key_names", {}), "links": set()}
        for c in op["columns"]:
            if c.get("references"):
                _link(state, name, c["references"]["table"])
        refresh_roles(state, name, conv)

    elif kind == "add_column":
        t, c = op["table"], op["column"]
        if need_table(t):
            if c["name"] in state[t]["columns"]:
                err(f"column {t}.{c['name']} already exists")
            ref = c.get("references")
            if ref:
                rt, rc = ref["table"], ref_column(state, ref)
                if rt not in state:
                    err(f"column {c['name']} references missing table {rt!r}")
                elif rc not in state[rt]["columns"]:
                    err(f"column {c['name']} references missing column {rt}.{rc}")
                state[t]["constraints"].add(fk_name(t, c["name"]))
            if ref:
                _link(state, t, ref["table"])
            if c.get("unique"):
                state[t]["constraints"].add(_trim(f"{t}_{c['name']}_key"))
                _add_key(state[t], _trim(f"{t}_{c['name']}_key"), [c["name"]])
            state[t]["columns"][c["name"]] = _col_entry(c)
            refresh_roles(state, t, conv)

    elif kind == "create_view":
        n = op["name"]
        if n in state or n in views_of(state):
            err(f"{n!r} already exists (a table or view has that name)")
        elif isinstance(state, State):
            state.views[n] = {"query": op["query"], "tables": {r for r in view_references(op["query"]) if r in state}}

    elif kind == "drop_view":
        n = op["name"]
        if n not in views_of(state):
            err(f"view {n!r} does not exist")
        else:
            del state.views[n]

    elif kind == "rename_table":
        t, n = op["table"], op["new_name"]
        if need_table(t):
            if n in state:
                err(f"table {n!r} already exists")
            else:
                state[n] = state.pop(t)
                for other in state[n].get("links", set()):
                    if other in state:
                        state[other]["links"].discard(t)
                        state[other]["links"].add(n)

    elif kind == "rename_column":
        t, c, n = op["table"], op["column"], op["new_name"]
        if need_col(t, c):
            if n in state[t]["columns"]:
                err(f"column {t}.{n} already exists")
            else:
                state[t]["columns"][n] = state[t]["columns"].pop(c)
                if state[t].get("pk") == c:
                    state[t]["pk"] = n
                ren = lambda k: frozenset(n if x == c else x for x in k)
                state[t]["keys"] = {ren(k) for k in state[t].get("keys", set())}
                state[t]["key_names"] = {nm: ren(k) for nm, k in state[t].get("key_names", {}).items()}
                refresh_roles(state, t, conv)

    elif kind == "alter_column_type":
        t, c = op["table"], op["column"]
        if need_col(t, c):
            state[t]["columns"][c]["type"] = normalize_type(op["type"])

    elif kind == "set_column_comment":
        need_col(op["table"], op["column"])

    elif kind in ("set_column_default", "drop_column_default", "set_not_null", "drop_not_null"):
        t, c = op["table"], op["column"]
        if need_col(t, c):
            entry = state[t]["columns"][c]
            if kind == "set_column_default":
                entry["default"] = op["default"]
            elif kind == "drop_column_default":
                entry["default"] = None
            elif kind == "set_not_null":
                entry["nullable"] = False
            elif pk_of(state, t) == c:
                err(f"column {t}.{c} is the primary key and cannot be made nullable")
            else:
                entry["nullable"] = True

    elif kind in ("add_index", "add_unique"):
        t = op["table"]
        if need_table(t):
            for c in op["columns"]:
                need_col(t, c)
            if kind == "add_unique":
                n = op.get("name") or unique_name(t, op["columns"])
                if n in state[t]["constraints"]:
                    err(f"constraint {n!r} already exists")
                state[t]["constraints"].add(n)
                _add_key(state[t], n, op["columns"])
            else:
                n = op.get("name") or index_name(t, op["columns"])
                if any(n in s["indexes"] for s in state.values()):
                    err(f"index {n!r} already exists")
                state[t]["indexes"].add(n)
                if op.get("unique"):
                    _add_key(state[t], n, op["columns"])

    elif kind == "add_foreign_key":
        t, c = op["table"], op["column"]
        rt = op["ref_table"]
        rc = op.get("ref_column") or pk_of(state, rt)
        if need_col(t, c):
            if rt not in state:
                err(f"referenced table {rt!r} does not exist")
            elif rc not in state[rt]["columns"]:
                err(f"referenced column {rt}.{rc} does not exist")
            n = op.get("name") or fk_name(t, c)
            if n in state[t]["constraints"]:
                err(f"constraint {n!r} already exists")
            state[t]["constraints"].add(n)
            _link(state, t, rt)

    elif kind == "add_check":
        t, n = op["table"], op["name"]
        if need_table(t):
            for problem in check_expression(op["expression"], set(state[t]["columns"])):
                err(problem)
            if n in state[t]["constraints"]:
                err(f"constraint {n!r} already exists")
            state[t]["constraints"].add(n)

    elif kind == "set_table_comment":
        if need_table(op["table"]):
            state[op["table"]]["comment"] = op["comment"]

    elif kind == "drop_column":
        t, c = op["table"], op["column"]
        if need_col(t, c):
            del state[t]["columns"][c]
            state[t]["keys"] = {k for k in state[t].get("keys", set()) if c not in k}
            state[t]["key_names"] = {nm: k for nm, k in state[t].get("key_names", {}).items() if c not in k}
            refresh_roles(state, t, conv)

    elif kind == "drop_table":
        t = op["table"]
        if need_table(t):
            _unlink_all(state, t)
            del state[t]

    elif kind == "drop_index":
        n = op["name"]
        owner = next((t for t, s in state.items() if n in s["indexes"]), None)
        if owner is None:
            err(f"index {n!r} does not exist")
        else:
            state[owner]["indexes"].discard(n)
            _drop_key_named(state[owner], n)

    elif kind == "drop_constraint":
        t, n = op["table"], op["name"]
        if need_table(t):
            if n not in state[t]["constraints"]:
                err(f"constraint {t}.{n} does not exist")
            state[t]["constraints"].discard(n)
            _drop_key_named(state[t], n)

    elif kind == "insert_rows":
        t = op["table"]
        if need_table(t):
            for r in op["rows"]:
                for k in r:
                    need_col(t, k)

    elif kind == "bulk_insert":
        t = op["table"]
        if need_table(t):
            refs = op.get("refs", [])
            carriers = {r["source_column"] for r in refs}
            for c in op["columns"]:
                if c not in carriers:
                    need_col(t, c)
            for r in refs:
                need_col(t, r["column"])
                if r["column"] in op["columns"]:
                    err(f"column {r['column']!r} is filled by a ref and must not also be a source column")
                pt = r["parent_table"]
                if pt not in state:
                    err(f"parent table {pt!r} does not exist")
                else:
                    need_col(pt, r["parent_key"])
                    if pk_of(state, pt) is None:
                        err(f"parent table {pt!r} has no primary key")

    elif kind == "update_rows":
        t = op["table"]
        if need_table(t):
            for k in {*op["where"], *op["set"]}:
                need_col(t, k)

    elif kind == "deactivate_rows":
        t = op["table"]
        if need_table(t):
            if role_of(state, t, "is_active") is None:
                err(f"table {t!r} has no column with the is_active role, so rows cannot be deactivated")
            for k in op["where"]:
                need_col(t, k)


def validate(ops: list[dict], state: dict, conv: Conventions | None = None) -> tuple[list[str], dict]:
    """Return (errors, state_after). `state` is not modified."""
    st = with_roles(state, conv) if conv is not None else copy.deepcopy(state)
    errors: list[str] = []
    for i, op in enumerate(ops):
        apply_op(op, st, errors, label=f"op[{i}] {op['op']}: ", conv=conv)
    return errors, st


def walk(ops: list[dict], state: dict, conv: Conventions | None = None) -> Iterator[tuple[dict, dict]]:
    """Yield (op, state_before_op) while advancing a private copy of the state."""
    st = with_roles(state, conv) if conv is not None else copy.deepcopy(state)
    for op in ops:
        yield op, copy.deepcopy(st)
        apply_op(op, st, conv=conv)
