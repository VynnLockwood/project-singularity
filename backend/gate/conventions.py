"""Names of the audit columns and primary keys, in one place.

New tables use snake_case audit columns and a primary key called ``<table>_id``.
Databases that have not been migrated yet still have ``id`` / camelCase columns,
so every lookup accepts either spelling (new one first). Remove the legacy names
once all databases are migrated.
"""
from __future__ import annotations

AUDIT_NAMES = {
    "created_at": ("created_at", "createdAt"),
    "updated_at": ("updated_at", "updatedAt"),
    "created_by": ("created_by", "createdBy"),
    "is_active": ("is_active", "isActive"),
}


def pk_name(table: str) -> str:
    """Primary key column a new table gets."""
    return f"{table}_id"


def audit_column(columns, logical: str) -> str | None:
    """Actual name of an audit column among `columns` (a mapping or collection), or None."""
    for name in AUDIT_NAMES[logical]:
        if name in columns:
            return name
    return None


def audit_names() -> set[str]:
    """Every spelling of every audit column (not the primary key)."""
    return {n for names in AUDIT_NAMES.values() for n in names}


def pk_column(columns, table: str, declared: str | None = None) -> str | None:
    """Primary key column of `table` given its column names: declared, then <table>_id, then id."""
    if declared and declared in columns:
        return declared
    for name in (pk_name(table), "id"):
        if name in columns:
            return name
    return None
