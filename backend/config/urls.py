from django.urls import include, path

from approvals import views as changes
from audit.views import audit_log
from chat import views as chat_views
from config.views import health
from schema import views as schema_views
from search import views as search_views
from settings_app import views as rules

urlpatterns = [
    path("api/health", health),
    path("api/changes", changes.changes),
    path("api/changes/<int:change_id>", changes.change_detail),
    path("api/changes/<int:change_id>/<str:action>", changes.change_action),
    path("api/queue", changes.queue),
    path("api/rules", rules.rules),
    path("api/rules/<int:rule_id>", rules.rule_detail),
    path("api/rules/<int:rule_id>/versions", rules.rule_versions),
    path("api/rules/<int:rule_id>/rollback", rules.rule_rollback),
    path("api/schema/tables", schema_views.tables),
    path("api/schema/graph", schema_views.graph),
    path("api/schema/tables/<str:name>", schema_views.table_detail),
    path("api/query", schema_views.run_query),
    path("api/search", search_views.search),
    path("api/search/reindex", search_views.reindex),
    path("api/audit", audit_log),
    path("api/chat/models", chat_views.models),
    path("api/chat/conversations", chat_views.conversations),
    path("api/chat/conversations/<int:conv_id>", chat_views.conversation_detail),
    path("api/chat/conversations/<int:conv_id>/messages", chat_views.send_message),
    path("api/", include("accounts.urls")),
]
