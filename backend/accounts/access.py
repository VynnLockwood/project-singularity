"""Per-database access: the one place that decides what a user can do on which database.

A database is a managed Postgres schema. A non-admin's level on database S is the grant for S, else the
`*` grant, else none; it is capped by the global role, and an inactive user has none. Admins reach every
database, present and future, without grants. A database the user cannot reach answers exactly like one
that does not exist (`introspect.UnknownSchema`, same text); `AccessDenied` is only for a database the
user can read but where they lack the level asked for.
"""
from __future__ import annotations

from collections.abc import Iterable

from schema import introspect

from .models import ROLE_RANK, Role, SchemaGrant

ALL = "*"

# ops that act on databases themselves and name them in their own fields (gate.operations.DATABASE_OPS)
_DATABASE_OP_FIELDS = {
    "create_schema": ("name",),
    "rename_schema": ("schema", "new_name"),
    "drop_schema": ("schema",),
    "move_table": ("from_schema", "to_schema"),
}


class AccessDenied(Exception):
    """The user can read the database but lacks the level for this action (a forbidden, not a missing one).

    The message is a role-style refusal that is safe to show; `status` is what web/MCP layers map it to.
    """

    status = 403

    def __init__(self, message: str, *, schema: str = "", level: str = "", role: str | None = None):
        super().__init__(message)
        self.schema = schema
        self.level = level
        self.role = role


def _rank(role) -> int | None:
    try:
        return ROLE_RANK[Role(role)]
    except ValueError:
        return None


def _active(user) -> bool:
    return bool(user is not None and getattr(user, "is_authenticated", False) and getattr(user, "is_active", False))


def _is_admin(user) -> bool:
    return getattr(user, "role", None) == Role.ADMIN


def _grants(user) -> dict[str, str]:
    """The user's stored grants, schema -> role (read once per decision)."""
    if not getattr(user, "pk", None):
        return {}
    return dict(SchemaGrant.objects.filter(user_id=user.pk).values_list("schema", "role"))


def _effective(user, schema, grants: dict[str, str]) -> str | None:
    if not _active(user) or not isinstance(schema, str) or not schema or schema == ALL:
        return None
    if _is_admin(user):
        return Role.ADMIN
    cap = _rank(getattr(user, "role", None))
    granted = grants.get(schema, grants.get(ALL))
    level = _rank(granted) if granted is not None else None
    if cap is None or level is None:
        return None
    return Role(granted) if level <= cap else Role(user.role)


def effective_role(user, schema: str) -> str | None:
    """The user's level (a `Role` value) on `schema`, or None when they cannot reach it.

    Does not check that the database exists (a `*` grant answers for any name); `require` and
    `readable_schemas` do.
    """
    return _effective(user, schema, _grants(user) if _active(user) and not _is_admin(user) else {})


def _meets(role: str | None, level: str) -> bool:
    need = _rank(level)
    have = _rank(role) if role is not None else None
    return need is not None and have is not None and have >= need


def can(user, schema: str, level: str) -> bool:
    """True when the user's effective level on `schema` is at least `level` (existence is not checked)."""
    return _meets(effective_role(user, schema), level)


def readable_schemas(user) -> list[str]:
    """The managed databases the user can read, master first (the order of `introspect.managed_schemas`)."""
    if not _active(user):
        return []
    grants = _grants(user) if not _is_admin(user) else {}
    return [m["name"] for m in introspect.managed_schemas() if _effective(user, m["name"], grants) is not None]


def _unknown(schema) -> introspect.UnknownSchema:
    # exactly what introspect.require_managed raises for a database that does not exist
    return introspect.UnknownSchema(f"Unknown schema {schema!r}.")


def require(user, schema: str, level: str = Role.VIEWER) -> str:
    """Return `schema` when the user has at least `level` on it.

    No effective level, or not a managed database: `introspect.UnknownSchema` (hidden equals missing).
    Readable but below `level`: `AccessDenied`.
    """
    if _rank(level) is None:
        raise ValueError(f"unknown level {level!r}")
    role = effective_role(user, schema)
    if role is None:
        raise _unknown(schema)
    introspect.require_managed(schema)
    if not _meets(role, level):
        raise AccessDenied(
            f"This needs the {Role(level).value} role on {schema!r}; you have {Role(role).value} there.",
            schema=schema, level=str(level), role=str(role))
    return schema


def affected_schemas(operations: Iterable[dict], default: str = "master") -> set[str]:
    """The databases a change's operations act on.

    Database ops name theirs (`create_schema` its `name`, `rename_schema` both names, `drop_schema` its
    `schema`, `move_table` both sides); every other op acts on its `schema` or on `default`. Never empty:
    a change that names nothing acts on `default`.
    """
    out: set[str] = set()
    for op in operations or ():
        if not isinstance(op, dict):
            continue
        fields = _DATABASE_OP_FIELDS.get(op.get("op"))
        if fields is not None:
            out.update(op[f] for f in fields if isinstance(op.get(f), str) and op.get(f))
        else:
            s = op.get("schema")
            out.add(s if isinstance(s, str) and s else default)
    return out or {default}
