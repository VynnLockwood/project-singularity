"""Shared set-up for the MCP tool tests: users of each role and a few ready-made changes."""
from accounts.models import Role, User
from mcp_server import tools


def make_users():
    return {r: User.objects.create(username=f"{r}@example.com", email=f"{r}@example.com", role=r)
            for r in (Role.VIEWER, Role.EDITOR, Role.APPROVER)}


def applied(approver, operations, schema="master"):
    """Propose and approve as the approver; returns the applied change."""
    ch = tools.propose_change(approver, operations, schema=schema)
    assert ch["status"] == "pending", ch
    done = tools.approve_change(approver, ch["id"])
    assert done["status"] == "applied", done
    return done


def create_table(name, *columns):
    return {"op": "create_table", "name": name,
            "columns": [{"name": c, "type": "text"} for c in (columns or ("name_en",))]}
