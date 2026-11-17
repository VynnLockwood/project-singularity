"""Per-database access: the one place that decides what a user can do on which database.

A database is a managed Postgres schema. A non-admin's level on database S is the grant for S, else the
`*` grant, else none; it is capped by the global role, and an inactive user has none. Admins reach every
database, present and future, without grants. A database the user cannot reach answers exactly like one
that does not exist (`introspect.UnknownSchema`, same text); `AccessDenied` is only for a database the
user can read but where they lack the level asked for. A specific grant with role `NONE` ("none") denies
that one database even under a `*` grant; `*` itself never holds "none" (no `*` row means none there).
"""
from __future__ import annotations

from collections.abc import Iterable

from schema import introspect

from .models import GRANT_NONE, ROLE_RANK, Role, SchemaGrant

ALL = "*"
NONE = GRANT_NONE

# ops that act on databases themselves and name them in their own fields (gate.operations.DATABASE_OPS):
# the fields naming databases that must exist, and those naming databases the op creates
_EXISTING_FIELDS = {
    "create_schema": (),
    "rename_schema": ("schema",),
    "drop_schema": ("schema",),
    "move_table": ("from_schema", "to_schema"),
}
_CREATED_FIELDS = {
    "create_schema": ("name",),
    "rename_schema": ("new_name",),
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


def _level_rank(level) -> int:
    """The rank of a level asked for; a misspelt level is a programming error, not a refusal."""
    rank = _rank(level)
    if rank is None:
        raise ValueError(f"unknown level {level!r}")
    return rank


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
    granted = grants[schema] if schema in grants else grants.get(ALL)  # a specific grant (even "none") wins
    level = _rank(granted) if granted is not None and granted != NONE else None
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
    need = _level_rank(level)
    have = _rank(role) if role is not None else None
    return have is not None and have >= need


def can(user, schema: str, level: str) -> bool:
    """True when the user's effective level on `schema` is at least `level` (existence is not checked).

    Raises ValueError for a misspelt level, like `require`.
    """
    _level_rank(level)
    return _meets(effective_role(user, schema), level)


def readable_schemas(user) -> list[str]:
    """The managed databases the user can read, master first (the order of `introspect.managed_schemas`)."""
    if not _active(user):
        return []
    grants = _grants(user) if not _is_admin(user) else {}
    return [m["name"] for m in introspect.managed_schemas() if _effective(user, m["name"], grants) is not None]


def effective_schemas(user) -> list[dict]:
    """Each managed database the user can read, with their level there: `[{"name", "role"}]`, master first.

    The same decision as `effective_role`, made once for the whole list (profile, /api/me, get_user_access).
    """
    if not _active(user):
        return []
    grants = _grants(user) if not _is_admin(user) else {}
    out = []
    for m in introspect.managed_schemas():
        role = _effective(user, m["name"], grants)
        if role is not None:
            out.append({"name": m["name"], "role": str(role)})
    return out


def reaches_all(user) -> bool:
    """True when the user reaches every database, present and future: an admin, or a `*` grant with no
    database denied under it (a cap below the grant still lowers the level, not the reach)."""
    if not _active(user):
        return False
    if _is_admin(user):
        return True
    grants = _grants(user)
    return ALL in grants and NONE not in grants.values()


def _unknown(schema) -> introspect.UnknownSchema:
    # exactly what introspect.require_managed raises for a database that does not exist
    return introspect.UnknownSchema(introspect.unknown_schema_message(schema))


def require(user, schema: str, level: str = Role.VIEWER) -> str:
    """Return `schema` when the user has at least `level` on it.

    No effective level, or not a managed database: `introspect.UnknownSchema` (hidden equals missing).
    Readable but below `level`: `AccessDenied`.
    """
    _level_rank(level)
    role = effective_role(user, schema)
    if role is None:
        raise _unknown(schema)
    introspect.require_managed(schema)
    if not _meets(role, level):
        raise AccessDenied(
            f"This needs the {Role(level).value} role on {schema!r}; you have {Role(role).value} there.",
            schema=schema, level=str(level), role=str(role))
    return schema


def _names(op: dict, fields) -> set[str]:
    return {op[f] for f in fields if isinstance(op.get(f), str) and op[f]}


def _split(operations, default: str) -> tuple[set[str], set[str]]:
    """(databases that must already exist, names the ops create) for a list of change operations."""
    existing: set[str] = set()
    created: set[str] = set()
    for op in operations if isinstance(operations, (list, tuple)) else ():
        if not isinstance(op, dict):
            existing.add(default)
            continue
        kind = op.get("op")
        kind = kind if isinstance(kind, str) else None
        if kind in _EXISTING_FIELDS:
            existing |= _names(op, _EXISTING_FIELDS[kind])
            created |= _names(op, _CREATED_FIELDS.get(kind, ()))
        else:
            # ordinary (and unknown) ops act on the change's database; an op-level `schema` can only add one
            existing.add(default)
            existing |= _names(op, ("schema",))
    return existing, created


def affected_schemas(operations: Iterable[dict], default: str = "master") -> set[str]:
    """Every database name a change's operations touch: `affected_existing` | `created_names`.

    Database ops name theirs (`create_schema` its `name`, `rename_schema` both names, `drop_schema` its
    `schema`, `move_table` both sides). Every other op, unknown kinds included, acts on `default` plus its
    own `schema` key if it has one (an op-level key can widen the set, never hide `default`). Never empty:
    a change that names nothing acts on `default`.

    Config ops (`set_user_role`, `set_setting`, `update_rule`, ... see `is_config_ops`) are NOT database
    ops: passed here they count as ordinary ops and give `{default}`. Callers must route config changes
    away first (they are decided by global role, never by grants).
    """
    existing, created = _split(operations, default)
    return (existing | created) or {default}


def affected_existing(operations: Iterable[dict], default: str = "master") -> set[str]:
    """The databases the ops act on that must already exist: `rename_schema.schema`, `drop_schema.schema`,
    `move_table.from_schema/to_schema`, and `default` (plus any op `schema`) for every other op.

    A change made only of `create_schema` ops gives the empty set (it acts on no existing database).
    An empty or malformed list gives `{default}`. Config ops: see `affected_schemas`.
    """
    existing, created = _split(operations, default)
    return existing if (existing or created) else {default}


def created_names(operations: Iterable[dict]) -> set[str]:
    """The database names the ops bring into being: `create_schema.name` and `rename_schema.new_name`."""
    return _split(operations, "master")[1]


def is_config_ops(operations) -> bool:
    """True when every op is a configuration op (`approvals.config_ops.CONFIG_OPS`): decided by global role."""
    from approvals.config_ops import CONFIG_OPS  # lazy: config_ops imports accounts.services

    return (isinstance(operations, (list, tuple)) and bool(operations)
            and all(isinstance(o, dict) and isinstance(o.get("op"), str) and o["op"] in CONFIG_OPS
                    for o in operations))
