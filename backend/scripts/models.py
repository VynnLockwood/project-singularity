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


class StagedLoad(models.Model):
    """Rows an AI client is sending in chunks over MCP, kept as COPY text on disk until finish_load."""

    class Status(models.TextChoices):
        OPEN = "open"
        FINISHED = "finished"
        CANCELLED = "cancelled"
        EXPIRED = "expired"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="staged_loads")
    schema = models.CharField(max_length=63)
    table = models.CharField(max_length=63)
    columns = models.JSONField(default=list)   # column names in the order the CSV carries them
    refs = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN, db_index=True)
    next_seq = models.PositiveIntegerField(default=1)
    rows = models.PositiveBigIntegerField(default=0)
    bytes = models.PositiveBigIntegerField(default=0)  # the truth about the file's length
    last_chunk_sha = models.CharField(max_length=64, blank=True)
    path = models.CharField(max_length=255, blank=True)  # file name under SCRIPT_DIR/loads; empty once removed
    script = models.ForeignKey(Script, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    change = models.ForeignKey("approvals.Change", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        ordering = ["-id"]


class UploadSession(models.Model):
    """A big script arriving in fixed-size chunks (web upload), streamed to a part file until complete."""

    class Status(models.TextChoices):
        OPEN = "open"
        COMPLETE = "complete"
        EXPIRED = "expired"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="upload_sessions")
    name = models.CharField(max_length=200)
    size = models.PositiveBigIntegerField()          # raw bytes the client will send
    chunk_bytes = models.PositiveIntegerField()
    received = models.JSONField(default=list)        # chunk numbers written so far
    path = models.CharField(max_length=255, blank=True)  # part file under SCRIPT_DIR/uploads; empty once removed
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN, db_index=True)
    script = models.ForeignKey(Script, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        ordering = ["-id"]
