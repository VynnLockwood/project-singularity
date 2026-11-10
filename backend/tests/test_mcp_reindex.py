"""MCP tool reindex_search."""
import pytest

from accounts.models import Role
from audit.models import AuditLog
from mcp_server import tools
from search import embedder
from search.embedder import SearchError
from search.models import DIMENSIONS

from .mcp_helpers import applied, create_table, make_users

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("master")]


@pytest.fixture
def users(settings, monkeypatch):
    settings.EMBED_URL = "http://embed.test"
    monkeypatch.setattr(embedder, "embed", lambda texts, kind: [[1.0] + [0.0] * (DIMENSIONS - 1) for _ in texts])
    u = make_users()
    applied(u[Role.APPROVER], [create_table("region")])
    return u


def test_reindex_is_for_approvers(users):
    assert not tools.allowed(users[Role.EDITOR], "reindex_search")
    assert tools.allowed(users[Role.APPROVER], "reindex_search")


def test_reindex_returns_counts_and_is_audited(users):
    out = tools.reindex_search(users[Role.APPROVER], rebuild=True)
    assert set(out) == {"embedded", "unchanged", "removed"} and out["embedded"] >= 1
    again = tools.reindex_search(users[Role.APPROVER])
    assert again["embedded"] == 0 and again["unchanged"] == out["embedded"]
    assert AuditLog.objects.filter(action="search.reindexed").count() == 2


def test_reindex_reports_an_embedding_failure(users, monkeypatch):
    def down(texts, kind):
        raise SearchError("the embed service is not reachable")
    monkeypatch.setattr(embedder, "embed", down)
    with pytest.raises(tools.ToolFailure, match="embed service is not reachable"):
        tools.reindex_search(users[Role.APPROVER], rebuild=True)
