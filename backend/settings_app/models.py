from django.conf import settings
from django.db import models


class Rule(models.Model):
    """A gate rule (spec section 2). Edited by approvers; every edit is versioned."""

    name = models.CharField(max_length=64, unique=True)
    type = models.CharField(max_length=32)
    params = models.JSONField(default=dict)
    severity = models.CharField(max_length=8, default="block")  # block | warn
    enabled = models.BooleanField(default=True)
    description = models.CharField(max_length=255, blank=True)
    version = models.PositiveIntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ["id"]

    def snapshot(self) -> dict:
        return {
            "name": self.name,
            "type": self.type,
            "params": self.params,
            "severity": self.severity,
            "enabled": self.enabled,
            "description": self.description,
        }


class RuleVersion(models.Model):
    """Immutable copy of a rule at one version (who, when, before/after via neighbours)."""

    rule = models.ForeignKey(Rule, on_delete=models.CASCADE, related_name="versions")
    version = models.PositiveIntegerField()
    snapshot = models.JSONField()
    action = models.CharField(max_length=16)  # created | updated | rolled_back
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-version"]
        unique_together = [("rule", "version")]


class Setting(models.Model):
    """A runtime override of one tunable value from singularity.yaml (see config.siconfig)."""

    key = models.CharField(max_length=64, unique=True)
    value = models.JSONField()
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
