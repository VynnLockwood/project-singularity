from django.conf import settings
from django.db import models


class DuplicateGroup(models.Model):
    """Records in one table of a managed schema that look like the same thing (found by the Rust engine)."""

    class Status(models.TextChoices):
        OPEN = "open"
        DISMISSED = "dismissed"  # a person said "not duplicates"; stays dismissed while the same group is found

    schema = models.CharField(max_length=63, default="master")
    table_name = models.CharField(max_length=63, db_index=True)
    fields = models.JSONField(default=list)       # the columns that were compared
    key = models.CharField(max_length=64)         # hash of table + member ids: the same group keeps its identity across scans
    member_ids = models.JSONField(default=list)
    score = models.FloatField()                   # weakest link between members
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    detected_at = models.DateTimeField(auto_now_add=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    merge_change = models.ForeignKey("approvals.Change", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        db_table = "duplicate_groups"
        constraints = [models.UniqueConstraint(fields=["schema", "table_name", "key"], name="duplicateGroupUnique")]
        ordering = ["-score", "id"]
