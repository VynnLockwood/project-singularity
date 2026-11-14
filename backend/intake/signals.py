"""Once an import's change is applied, its mapping becomes the recipe for that file shape in that database."""
from django.db.models.signals import post_save
from django.dispatch import receiver

from approvals.models import Change

from . import match
from .models import ImportRecipe, ImportRun


@receiver(post_save, sender=Change)
def import_change_applied(sender, instance, **kwargs):
    if instance.status != Change.Status.APPLIED:
        return
    run = ImportRun.objects.filter(change=instance).first()
    if run is None:
        return
    headers = [h for s in run.sources or [] for h in s.get("headers") or []]
    plan = run.plan or {}
    recipe, _ = ImportRecipe.objects.update_or_create(
        schema=run.schema, fingerprint=match.fingerprint(headers),
        defaults={"headers": headers, "mapping": plan.get("mapping") or {}, "filters": plan.get("filters") or {},
                  "approved_change": instance, "created_by": run.owner})
    if run.recipe_id != recipe.pk:
        ImportRun.objects.filter(pk=run.pk).update(recipe=recipe)
