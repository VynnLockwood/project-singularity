from django.conf import settings
from django.db import models
from django.utils import timezone


class Script(models.Model):
    """An uploaded SQL script kept on disk until it expires (spec section 2, storage/expiry)."""

    class Status(models.TextChoices):
        UPLOADED = "uploaded"
        VALIDATED = "validated"
        SUBMITTED = "submitted"
        EXPIRED = "expired"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="scripts")
    name = models.CharField(max_length=200)  # display only; never used as a file name
    sha256 = models.CharField(max_length=64)  # of the stored (normalized) bytes
    size = models.PositiveBigIntegerField()  # stored (normalized) bytes
    path = models.CharField(max_length=255)  # file name under SCRIPT_DIR; empty once purged
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.UPLOADED, db_index=True)
    report = models.JSONField(default=dict, blank=True)
    mapping = models.JSONField(default=dict, blank=True)
    change = models.ForeignKey("approvals.Change", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        ordering = ["-id"]
