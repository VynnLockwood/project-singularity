from django.conf import settings
from django.db import models


class Change(models.Model):
    """One proposed change to master data or structure (spec section 2, lifecycle)."""

    class Status(models.TextChoices):
        PENDING = "pending"    # checked, waiting for a decision
        REJECTED = "rejected"  # blocked by the gate, or declined by a person
        APPLIED = "applied"
        FAILED = "failed"      # approved but the database refused; nothing was changed
        STALE = "stale"        # schema or rules moved since the check; needs a re-check

    requester = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="changes")
    source = models.CharField(max_length=16)  # web | mcp | grid | revert
    prompt = models.TextField(blank=True)
    conversation_id = models.PositiveIntegerField(null=True, blank=True)
    kind = models.CharField(max_length=8)  # schema | data
    schema = models.CharField(max_length=63, default="master")  # the managed schema this change targets
    status = models.CharField(max_length=12, choices=Status.choices, db_index=True)

    original_operations = models.JSONField(default=list)
    operations = models.JSONField(default=list)  # after the gate (auto-added columns included)
    check_result = models.JSONField(default=dict)
    generated_sql = models.JSONField(default=list)
    diff = models.JSONField(default=list)
    affected_tables = models.JSONField(default=list)
    fingerprint = models.CharField(max_length=64, blank=True)
    rules_hash = models.CharField(max_length=64, blank=True)

    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)

    reverse_operations = models.JSONField(null=True, blank=True)  # None = cannot be undone
    reverts = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="reverted_by"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]
        indexes = [models.Index(fields=["status", "kind"])]
