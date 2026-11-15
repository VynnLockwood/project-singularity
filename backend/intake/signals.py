"""Once an import's change is applied, its mapping becomes the recipe for that file shape in that database.

A recipe only moves forward: saving an older applied change again (a note, a revert marker) never replaces the
recipe a newer import approved. The signal never stops the approval: a failure here is logged, not raised."""
import logging

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from approvals.models import Change

from . import match
from .models import ImportRecipe, ImportRun

log = logging.getLogger(__name__)


def _newer(run: ImportRun, recipe: ImportRecipe) -> bool:
    """May this run's applied change replace the recipe? Yes when the recipe has no approved change, is its own,
    or came from an older import run (by run, else by change id when that run is gone)."""
    if recipe.approved_change_id is None or recipe.approved_change_id == run.change_id:
        return True
    other = ImportRun.objects.filter(change_id=recipe.approved_change_id).values_list("pk", flat=True).first()
    return run.pk > other if other is not None else run.change_id > recipe.approved_change_id


@receiver(post_save, sender=Change)
def import_change_applied(sender, instance, **kwargs):
    if instance.status != Change.Status.APPLIED:
        return
    try:
        with transaction.atomic():      # a savepoint: a failure here leaves the approval's transaction intact
            _save_recipe(instance)
    except Exception:  # noqa: BLE001 - the recipe is a convenience; the applied change must stand
        log.exception("could not save the import recipe for change #%s", instance.pk)


def _save_recipe(change: Change) -> None:
    run = ImportRun.objects.filter(change=change).first()
    if run is None:
        return
    headers = [h for s in run.sources or [] for h in s.get("headers") or []]
    fp = match.fingerprint(headers)
    current = ImportRecipe.objects.select_for_update().filter(schema=run.schema, fingerprint=fp).first()
    if current is not None and not _newer(run, current):
        return
    plan = run.plan or {}
    recipe, _ = ImportRecipe.objects.update_or_create(
        schema=run.schema, fingerprint=fp,
        defaults={"headers": headers, "mapping": plan.get("mapping") or {}, "filters": plan.get("filters") or {},
                  "approved_change": change, "created_by": run.owner})
    if run.recipe_id != recipe.pk:
        ImportRun.objects.filter(pk=run.pk).update(recipe=recipe)
