from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from approvals import service
from approvals.models import Change

from . import events


@receiver(post_save, sender=Change)
def change_saved(sender, instance, **kwargs):
    """Tell the requester and all approvers whenever a change is created or its status moves."""
    change_id = instance.pk

    def send():
        change = Change.objects.select_related("requester", "decided_by").get(pk=change_id)
        payload = {"type": "change", "change": service.serialize(change)}
        events.publish([events.user_group(change.requester_id), events.APPROVERS], payload)

    transaction.on_commit(send)
