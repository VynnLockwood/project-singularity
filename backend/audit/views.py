from django.http import JsonResponse

from accounts.api import endpoint, limit_param

from .models import as_dict, entries


@endpoint("GET", min_role="approver")  # entries hold invited emails, roles and prompts: same as MCP get_audit_log
def audit_log(request):
    # entries about a database the user is not approver on are left out, before the limit
    items, _ = entries(action=request.GET.get("action"), limit=limit_param(request, default=100, maximum=500),
                       viewer=request.user)
    return JsonResponse({"entries": [as_dict(a) for a in items]})
