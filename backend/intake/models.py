from django.conf import settings
from django.db import models
from django.utils import timezone


class ImportRecipe(models.Model):
    """A mapping that worked for a file shape (headers fingerprint), reused for the next file of that shape."""

    schema = models.CharField(max_length=63)
    fingerprint = models.CharField(max_length=64)  # sha256 hex
    headers = models.JSONField(default=list)
    mapping = models.JSONField(default=dict)
    filters = models.JSONField(default=list)
    approved_change = models.ForeignKey("approvals.Change", null=True, blank=True, on_delete=models.SET_NULL,
                                        related_name="+")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "intake_recipes"
        constraints = [models.UniqueConstraint(fields=["schema", "fingerprint"], name="intakeRecipeUnique")]


class ImportRun(models.Model):
    """One AI-assisted import: a raw file plus the person's prompt, taken through profiling to a ready change."""

    class State(models.TextChoices):
        PROFILING = "profiling"
        MATCHING = "matching"
        DESIGNING = "designing"
        WAITING_FOR_ANSWER = "waiting_for_answer"
        CHECKING = "checking"
        READY = "ready"
        FAILED = "failed"
        SUBMITTED = "submitted"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="import_runs")
    schema = models.CharField(max_length=63)
    prompt = models.TextField(blank=True)
    file_path = models.CharField(max_length=500)
    file_name = models.CharField(max_length=200)
    state = models.CharField(max_length=20, choices=State.choices, default=State.PROFILING, db_index=True)
    round = models.IntegerField(default=0)
    sources = models.JSONField(default=list)   # reader output, without rows
    profile = models.JSONField(default=dict)
    matches = models.JSONField(default=dict)
    plan = models.JSONField(default=dict)      # mapping, filters, reasons, questions, skips
    report = models.JSONField(default=dict)
    answers = models.JSONField(default=list)
    heartbeat = models.DateTimeField(null=True, blank=True)
    change = models.ForeignKey("approvals.Change", null=True, blank=True, on_delete=models.SET_NULL,
                               related_name="+")
    recipe = models.ForeignKey(ImportRecipe, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        db_table = "intake_runs"
        ordering = ["-id"]


class ImportMessage(models.Model):
    class Role(models.TextChoices):
        PROFILER = "profiler"
        MATCHER = "matcher"
        DESIGNER = "designer"
        CRITIC = "critic"
        PERSON = "person"
        SYSTEM = "system"

    run = models.ForeignKey(ImportRun, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=10, choices=Role.choices)
    text = models.CharField(max_length=2000)
    details = models.JSONField(default=dict)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "intake_messages"
        ordering = ["id"]
