from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from approvals.models import Change

from . import indexer


@receiver(post_save, sender=Change)
def schema_change_applied(sender, instance, **kwargs):
    if instance.status == Change.Status.APPLIED and instance.kind in ("schema", "sql"):
        transaction.on_commit(indexer.schedule_reindex)
