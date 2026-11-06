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
    "insert_rows{table,rows[]}, update_rows{table,where{},set{}}, deactivate_rows{table,where{}}. "
    "Types: text, integer, bigint, smallint, boolean, date, timestamptz, timestamp, uuid, jsonb, real, "
    "double precision, numeric, numeric(p,s), varchar(n). Audit columns are added automatically."
)

SPECS = {
    "get_rules": ("The team's database design rules. Read these before designing tables.", {}, []),
    "list_tables": ("Tables in the master data database, with comments and row estimates.", {}, []),
    "describe_table": ("Columns, types, constraints and indexes of one table.",
                       {"table": {"type": "string"}}, ["table"]),
    "query": ("Run one read-only SELECT against master data. Row-limited.",
              {"sql": {"type": "string"}, "limit": {"type": "integer"}}, ["sql"]),
    "search_schema": ("Find where a kind of data belongs: semantic search over tables and columns (Thai or English). "
                      "Use before proposing a new column. If 'confident' is false, nothing fits well.",
                      {"query": {"type": "string"}, "limit": {"type": "integer"}}, ["query"]),
    "propose_change": (
        "Propose structural or data changes as a list of operations (never SQL). " + _OPS +
        " Returns the checked change with a diff. The user approves it in a popup; nothing is applied until then.",
        {"operations": {"type": "array", "items": {"type": "object"}},
         "prompt": {"type": "string", "description": "the user's request, in their words"}},
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
