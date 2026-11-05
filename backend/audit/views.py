from django.http import JsonResponse

from accounts.api import endpoint, limit_param

from .models import AuditLog


@endpoint("GET")
def audit_log(request):
    qs = AuditLog.objects.all()
    if action := request.GET.get("action"):
        qs = qs.filter(action=action)
    return JsonResponse({"entries": [
        {"id": a.pk, "at": a.created_at.isoformat(), "actor": a.actor_email, "action": a.action,
         "target": a.target, "details": a.details}
        for a in qs[: limit_param(request, default=100, maximum=500)]
    ]})
