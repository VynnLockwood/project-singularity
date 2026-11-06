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
from .operations import check_expression, normalize_type


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
    return {"type": normalize_type(col["type"]), "nullable": nullable}


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
        for c in op["columns"]:
            cols[c["name"]] = _col_entry(c)
            if c.get("primary_key"):
                pk = c["name"]
            if c.get("unique"):
                constraints.add(_trim(f"{name}_{c['name']}_key"))
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
                       "comment": op.get("comment")}
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
            if c.get("unique"):
                state[t]["constraints"].add(_trim(f"{t}_{c['name']}_key"))
            state[t]["columns"][c["name"]] = _col_entry(c)
            refresh_roles(state, t, conv)

    elif kind == "rename_table":
        t, n = op["table"], op["new_name"]
        if need_table(t):
            if n in state:
                err(f"table {n!r} already exists")
            else:
                state[n] = state.pop(t)

    elif kind == "rename_column":
        t, c, n = op["table"], op["column"], op["new_name"]
        if need_col(t, c):
            if n in state[t]["columns"]:
                err(f"column {t}.{n} already exists")
            else:
                state[t]["columns"][n] = state[t]["columns"].pop(c)
                if state[t].get("pk") == c:
                    state[t]["pk"] = n
                refresh_roles(state, t, conv)

    elif kind == "alter_column_type":
        t, c = op["table"], op["column"]
        if need_col(t, c):
            state[t]["columns"][c]["type"] = normalize_type(op["type"])

    elif kind == "set_column_comment":
        need_col(op["table"], op["column"])

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
            else:
                n = op.get("name") or index_name(t, op["columns"])
                if any(n in s["indexes"] for s in state.values()):
                    err(f"index {n!r} already exists")
                state[t]["indexes"].add(n)

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
            refresh_roles(state, t, conv)

    elif kind == "drop_table":
        t = op["table"]
        if need_table(t):
            del state[t]

    elif kind == "drop_index":
        n = op["name"]
        owner = next((t for t, s in state.items() if n in s["indexes"]), None)
        if owner is None:
            err(f"index {n!r} does not exist")
        else:
            state[owner]["indexes"].discard(n)

    elif kind == "drop_constraint":
        t, n = op["table"], op["name"]
        if need_table(t):
            if n not in state[t]["constraints"]:
                err(f"constraint {t}.{n} does not exist")
            state[t]["constraints"].discard(n)

    elif kind == "insert_rows":
        t = op["table"]
        if need_table(t):
            for r in op["rows"]:
                for k in r:
                    need_col(t, k)

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
