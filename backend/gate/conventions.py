"""Which column plays which part, read from the rules (never from names in code).

The ``required_columns`` rule (``auditColumns``) tags the columns the engine needs to
understand with a ``role``::

    {"name": "{table}_id", "role": "primary_key", "aliases": ["id"], ...}
    {"name": "updated_at", "role": "updated_at", "aliases": ["updatedAt"], ...}

``name`` may contain ``{table}``. ``aliases`` are older spellings that still count while a
database is being migrated to a new convention. Change the rule through the MCP and the
engine follows; nothing here knows a column name.

Roles: primary_key, created_at, updated_at, created_by, is_active.
"""
from __future__ import annotations

import copy

ROLES = ("primary_key", "created_at", "updated_at", "created_by", "is_active")
RULE_NAME = "auditColumns"
# keys that describe a required column to the engine but are not part of a column definition
META_KEYS = ("role", "aliases")


class Conventions:
    """Role -> (name template, aliases), built from a list of rules."""

    def __init__(self, columns: list[dict] | None = None):
        self.roles: dict[str, dict] = {}
        for c in columns or []:
            role = c.get("role")
            if role in ROLES and role not in self.roles:
                self.roles[role] = {"name": c["name"], "aliases": list(c.get("aliases") or [])}

    @classmethod
    def from_rules(cls, rules: list[dict] | None) -> "Conventions":
        for r in rules or []:
            if r.get("type") == "required_columns" and r.get("enabled", True):
                return cls(r["params"].get("columns"))
        return cls()

    def candidates(self, role: str, table: str) -> list[str]:
        spec = self.roles.get(role)
        if not spec:
            return []
        return [n.replace("{table}", table) for n in [spec["name"], *spec["aliases"]]]

    def resolve(self, role: str, table: str, columns, declared_pk: str | None = None) -> str | None:
        """Actual column of `table` that plays `role`, or None. The database's own primary key wins."""
        if role == "primary_key" and declared_pk:
            return declared_pk
        for name in self.candidates(role, table):
            if name in columns:
                return name
        return None

    def table_roles(self, table: str, columns, declared_pk: str | None = None) -> dict[str, str]:
        out = {}
        for role in ROLES:
            name = self.resolve(role, table, columns, declared_pk)
            if name:
                out[role] = name
        return out

    def every_name(self) -> set[str]:
        """Every spelling of the non-key roles (used to hide audit columns from searches and grids)."""
        return {
            n for role, spec in self.roles.items() if role != "primary_key"
            for n in [spec["name"], *spec["aliases"]] if "{table}" not in n
        }


def strip_meta(column: dict) -> dict:
    """A column definition without the role tags (what actually goes into create_table)."""
    c = copy.deepcopy(column)
    for k in META_KEYS:
        c.pop(k, None)
    return c
