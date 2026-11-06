from django.http import JsonResponse

from accounts.api import endpoint, error

from . import services
from .models import Rule


def rule_json(r: Rule) -> dict:
    return {**r.snapshot(), "id": r.pk, "version": r.version, "updatedAt": r.updated_at.isoformat()}


@endpoint(["GET", "POST"])
def rules(request):
    if request.method == "GET":
        return JsonResponse({"rules": [rule_json(r) for r in Rule.objects.all()]})
    if not request.user.can_approve:
        return error("forbidden", 403)
    b = request.json
    try:
        rule = services.create_rule(
            request.user, name=b.get("name"), type=b.get("type"), params=b.get("params"),
            severity=b.get("severity", "block"), enabled=b.get("enabled", True),
            description=str(b.get("description", "")),
        )
    except services.RuleError as exc:
        return error("invalid rule", 400, errors=exc.errors)
    return JsonResponse(rule_json(rule), status=201)


@endpoint(["GET", "PATCH"])
def rule_detail(request, rule_id):
    rule = Rule.objects.filter(pk=rule_id).first()
    if rule is None:
        return error("not found", 404)
    if request.method == "GET":
        return JsonResponse(rule_json(rule))
    if not request.user.can_approve:
        return error("forbidden", 403)
    try:
        services.update_rule(request.user, rule, **request.json)
    except services.RuleError as exc:
        return error("invalid rule", 400, errors=exc.errors)
    rule.refresh_from_db()
    return JsonResponse(rule_json(rule))


@endpoint("GET")
def rule_versions(request, rule_id):
    rule = Rule.objects.filter(pk=rule_id).first()
    if rule is None:
        return error("not found", 404)
    return JsonResponse({"versions": [
        {"version": v.version, "action": v.action, "snapshot": v.snapshot,
         "changedBy": v.changed_by.email if v.changed_by_id else None, "createdAt": v.created_at.isoformat()}
        for v in rule.versions.select_related("changed_by")
    ]})


@endpoint("POST", min_role="approver")
def rule_rollback(request, rule_id):
    rule = Rule.objects.filter(pk=rule_id).first()
    if rule is None:
        return error("not found", 404)
    try:
        services.rollback_rule(request.user, rule, request.json.get("version"))
    except services.RuleError as exc:
        return error("cannot roll back", 400, errors=exc.errors)
    rule.refresh_from_db()
    return JsonResponse(rule_json(rule))


@endpoint(["GET", "PATCH"])
def settings(request):
    from . import tunables

    if request.method == "GET":
        return JsonResponse({"settings": tunables.describe()})
    if not request.user.can_approve:
        return error("forbidden", 403)
    try:
        return JsonResponse({"settings": tunables.apply(request.user, request.json)})
    except tunables.TunableError as exc:
        return error("invalid settings", 400, errors=exc.errors)
