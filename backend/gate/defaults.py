"""Default master-data rules, seeded once into singularity.rules (editable in the UI)."""

RESERVED = ["user", "order", "group", "table", "column", "select", "from", "where", "index", "primary", "check"]

REQUIRED = [
    {"name": "{table}_id", "type": "integer", "primary_key": True, "identity": True, "nullable": False},
    {"name": "created_at", "type": "timestamptz", "nullable": False, "default": "now()"},
    {"name": "updated_at", "type": "timestamptz", "nullable": False, "default": "now()"},
    {"name": "created_by", "type": "text"},
    {"name": "is_active", "type": "boolean", "nullable": False, "default": True},
]

DEFAULT_RULES = [
    {
        "name": "tableNaming",
        "type": "naming",
        "params": {"target": "table", "style": "snake_case", "maxLength": 63, "reserved": RESERVED},
        "severity": "block",
        "description": "Table names are snake_case.",
    },
    {
        "name": "columnNaming",
        "type": "naming",
        "params": {"target": "column", "style": "snake_case", "maxLength": 63, "reserved": RESERVED},
        "severity": "block",
        "description": "Column names are snake_case.",
    },
    {
        "name": "auditColumns",
        "type": "required_columns",
        "params": {"columns": REQUIRED, "autoAdd": True},
        "severity": "block",
        "description": "Every table has <table>_id, created_at, updated_at, created_by and is_active (added automatically).",
    },
    {
        "name": "careWithDrops",
        "type": "forbidden",
        "params": {"ops": ["drop_table", "drop_column"]},
        "severity": "warn",
        "description": "Dropping a table or column destroys data. Check the reason and that a backup exists.",
    },
    {
        "name": "moneyIsNumeric",
        "type": "type_map",
        "params": {
            "columnPattern": "(?i)(price|amount|cost|balance|total|fee)$",
            "allowedTypes": ["numeric(18,2)"],
        },
        "severity": "block",
        "description": "Money columns use numeric(18,2), never floating point.",
    },
    {
        "name": "timestampsHaveTimezone",
        "type": "type_map",
        "params": {"columnPattern": "_at$", "allowedTypes": ["timestamptz"]},
        "severity": "block",
        "description": "Columns ending in _at use timestamptz.",
    },
    {
        "name": "noFloat",
        "type": "type_map",
        "params": {"columnPattern": ".*", "forbiddenTypes": ["real", "double precision"]},
        "severity": "block",
        "description": "No floating-point columns in master data.",
    },
    {
        "name": "codesNeedLookup",
        "type": "reference_for_codes",
        "params": {"pattern": ".+_code$"},
        "severity": "warn",
        "description": "Code-like text columns should reference a lookup table.",
    },
    {
        "name": "foreignKeyNaming",
        "type": "fk_naming",
        "params": {"style": "snake_case"},
        "severity": "warn",
        "description": "Foreign key columns are named <referenced_table>_id.",
    },
    {
        "name": "needsBusinessKey",
        "type": "business_key",
        "params": {},
        "severity": "warn",
        "description": "Every table has a unique business key besides the primary key.",
    },
]
