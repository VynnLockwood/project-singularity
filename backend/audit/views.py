from django.http import JsonResponse

from accounts.api import endpoint, limit_param

from .models import as_dict, entries


@endpoint("GET")
def audit_log(request):
    items, _ = entries(action=request.GET.get("action"), limit=limit_param(request, default=100, maximum=500))
    return JsonResponse({"entries": [as_dict(a) for a in items]})
