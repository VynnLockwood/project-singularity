from django.conf import settings
from django.db import models


class AuditLog(models.Model):
    """Append-only record of who did what (spec section 2, audit)."""

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    actor_email = models.EmailField(blank=True)  # kept even if the user row is removed
    action = models.CharField(max_length=64)
    target = models.CharField(max_length=255, blank=True)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["action", "-created_at"])]


def record(actor, action: str, target: str = "", details: dict | None = None) -> AuditLog:
    return AuditLog.objects.create(
        actor=actor if getattr(actor, "pk", None) else None,
        actor_email=getattr(actor, "email", "") or "",
        action=action,
        target=target,
        details=details or {},
    )
