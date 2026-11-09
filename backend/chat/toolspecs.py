"""Tool definitions the model sees in the web chat (OpenAI function format).

Approval is not a tool here: the app shows an Approve / Reject popup, so the model cannot approve
its own proposals.
"""
from mcp_server import tools as mcp_tools

_OPS = (
    "Each operation is an object with an 'op': "
    "create_table{name,columns[{name,type,nullable?,unique?,default?,comment?,references?{table,column?}}],comment?}, "
    "add_column{table,column}, rename_table{table,new_name}, rename_column{table,column,new_name}, "
    "alter_column_type{table,column,type}, set_column_comment{table,column,comment}, add_index{table,columns,unique?}, "
    "add_unique{table,columns}, add_foreign_key{table,column,ref_table,ref_column?,on_delete?(restrict|cascade|set null|no action)}, "
    "add_check{table,name,expression}, set_table_comment{table,comment}, "
    "drop_column{table,column,reason}, drop_table{table,reason}, drop_index{name,reason}, drop_constraint{table,name,reason}, "
    "insert_rows{table,rows[]}, update_rows{table,where{},set{}}, deactivate_rows{table,where{}}, "
    "create_schema{name,comment?}, rename_schema{schema,new_name}, drop_schema{schema,reason}. "
    "Schema ops go alone, in their own change; a 'database' is a Postgres schema. "
    "(bulk_insert exists but is produced by script intake only; never write it yourself, the gate refuses it.) "
    "Types: text, integer, bigint, smallint, boolean, date, timestamptz, timestamp, uuid, jsonb, real, "
    "double precision, numeric, numeric(p,s), varchar(n). Audit columns are added automatically."
)

SCHEMA = {"schema": {"type": "string", "description": "the database (Postgres schema) to use; default master. See list_schemas"}}

SPECS = {
    "get_rules": ("The team's database design rules. Read these before designing tables.", {}, []),
    "list_schemas": ("The databases (a 'database' is a Postgres schema) with table counts and comments. master is the default.", {}, []),
    "list_tables": ("Tables in one database (schema, default master), with comments and row estimates.", {**SCHEMA}, []),
    "describe_table": ("Columns, types, constraints and indexes of one table.",
                       {"table": {"type": "string"}, **SCHEMA}, ["table"]),
    "query": ("Run one read-only SELECT against one database (schema, default master). Row-limited.",
              {"sql": {"type": "string"}, "limit": {"type": "integer"}, **SCHEMA}, ["sql"]),
    "search_schema": ("Find where a kind of data belongs: semantic search over tables and columns (Thai or English). "
                      "Use before proposing a new column. If 'confident' is false, nothing fits well.",
                      {"query": {"type": "string"}, "limit": {"type": "integer"},
                       "schema": {"type": "string", "description": "limit to one database; omit to search all"}}, ["query"]),
    "propose_change": (
        "Propose structural or data changes as a list of operations (never SQL). " + _OPS +
        " Returns the checked change with a diff. The user approves it in a popup; nothing is applied until then.",
        {"operations": {"type": "array", "items": {"type": "object"}},
         "prompt": {"type": "string", "description": "the user's request, in their words"},
         **SCHEMA},
        ["operations"]),
    "get_settings": ("The quality settings with their ranges, and every gate rule including switched-off ones.", {}, []),
    "propose_config_change": (
        "Propose a change to settings, gate rules or roles. Important, so it always waits for an approver in the approval queue. "
        "%s" % """Configuration operations (settings, rules, roles). Each is an object with an 'op': set_setting{key,value}, reset_setting{key}, update_rule{name,severity?('block'|'warn'),enabled?,params?,description?}, create_rule{name,type,params,severity?,enabled?,description?}, set_user_role{email,role('viewer'|'editor'|'approver')}. Call get_settings first for the keys, allowed ranges and current rules. Never mix these with table or data operations.""",
        {"operations": {"type": "array", "items": {"type": "object"}},
         "prompt": {"type": "string", "description": "the user's request, in their words"}},
        ["operations"]),
    "list_users": ("People who can sign in, with their roles.", {}, []),
    "get_change": ("Status, diff and any errors of a proposed change.",
                   {"change_id": {"type": "integer"}}, ["change_id"]),
    "list_pending": ("Changes waiting for an approver.", {}, []),
    "submit_script": (
        "Upload a small pg_dump-style SQL script inline, validate it against the gate and submit it as one "
        "approvable Change. No raw SQL is executed. Big scripts: tell the user to upload the file in the web app.",
        {"name": {"type": "string", "description": "file name to show (e.g. master.sql)"},
         "content": {"type": "string", "description": "the whole SQL file contents"},
         "mapping": {"type": "object", "description": "optional table/column mapping; omit to auto-suggest"}},
        ["name", "content"]),
    "validate_script": (
        "Re-validate an uploaded script and return the trimmed report (errors with line numbers, gate result, "
        "orphan samples, dry-run rows).",
        {"script_id": {"type": "integer"}, "mapping": {"type": "object"}}, ["script_id"]),
    "get_script": (
        "Status, stored report and change link of an uploaded/submitted script.",
        {"script_id": {"type": "integer"}}, ["script_id"]),
}


def specs_for(user) -> list[dict]:
    out = []
    for name, (desc, props, required) in SPECS.items():
        if mcp_tools.allowed(user, name):
            out.append({"type": "function", "function": {
                "name": name, "description": desc,
                "parameters": {"type": "object", "properties": props, "required": required},
            }})
    return out
