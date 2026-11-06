"""Which column of a real table plays which role (primary key, updated_at, is_active, ...).

The primary key comes from the database itself; the other roles come from the live
``auditColumns`` rule, so changing that rule through the MCP changes what the app understands.
"""
from __future__ import annotations

import re

from gate.conventions import Conventions


def conventions() -> Conventions:
    from settings_app.services import active_rules

    return Conventions.from_rules(active_rules())


def primary_key_of(described: dict) -> str | None:
    """Primary key column named in an introspect.describe_table result."""
    for c in described["constraints"]:
        if c["kind"] == "primary_key":
            m = re.search(r'\("?([^")]+)"?\)', c["definition"])
            return m.group(1) if m else None
    return None


def roles_of(described: dict, conv: Conventions | None = None) -> dict[str, str]:
    conv = conv or conventions()
    cols = {c["name"] for c in described["columns"]}
    return conv.table_roles(described["name"], cols, primary_key_of(described))


def hidden_columns(conv: Conventions | None = None) -> set[str]:
    """Names of audit columns (every spelling) that grids and searches should not treat as data."""
    return (conv or conventions()).every_name()
