from django.urls import include, path, re_path

from accounts import oauth_views as oauth
from approvals import views as changes
from audit.views import audit_log
from chat import views as chat_views
from config.views import health
from duplicates import views as dup
from schema import views as schema_views
from search import views as search_views
from scripts import api as scripts_api
from settings_app import views as rules

urlpatterns = [
    re_path(r"^\.well-known/oauth-protected-resource(?:/(?P<suffix>.*))?$", oauth.protected_resource),
    re_path(r"^\.well-known/(?:oauth-authorization-server|openid-configuration)(?:/(?P<suffix>.*))?$", oauth.authorization_server),
    path("oauth/register", oauth.register),
    path("oauth/authorize", oauth.authorize),
    path("oauth/token", oauth.token),
    path("api/health", health),
    path("api/changes", changes.changes),
    path("api/changes/<int:change_id>", changes.change_detail),
    path("api/changes/<int:change_id>/<str:action>", changes.change_action),
    path("api/queue", changes.queue),
    path("api/settings", rules.settings),
    path("api/rules", rules.rules),
    path("api/rules/<int:rule_id>", rules.rule_detail),
    path("api/rules/<int:rule_id>/versions", rules.rule_versions),
    path("api/rules/<int:rule_id>/rollback", rules.rule_rollback),
    path("api/schema/tables", schema_views.tables),
    path("api/schema/graph", schema_views.graph),
    path("api/schema/tables/<str:name>", schema_views.table_detail),
    path("api/schema/tables/<str:name>/rows", schema_views.table_rows),
    path("api/schema/tables/<str:name>/options", schema_views.table_options),
    path("api/query", schema_views.run_query),
    path("api/duplicates", dup.groups),
    path("api/duplicates/scan", dup.scan),
    path("api/duplicates/<int:group_id>", dup.group_detail),
    path("api/duplicates/<int:group_id>/dismiss", dup.dismiss),
    path("api/duplicates/<int:group_id>/reopen", dup.reopen),
    path("api/duplicates/<int:group_id>/merge", dup.merge),
    path("api/search", search_views.search),
    path("api/search/reindex", search_views.reindex),
    path("api/audit", audit_log),
    path("api/chat/models", chat_views.models),
    path("api/chat/conversations", chat_views.conversations),
    path("api/chat/conversations/<int:conv_id>", chat_views.conversation_detail),
    path("api/chat/conversations/<int:conv_id>/messages", chat_views.send_message),
    path("api/scripts", scripts_api.scripts),
    path("api/scripts/<int:script_id>", scripts_api.script_detail),
    path("api/scripts/<int:script_id>/validate", scripts_api.validate_view),
    path("api/scripts/<int:script_id>/submit", scripts_api.submit_view),
    path("api/", include("accounts.urls")),
]
