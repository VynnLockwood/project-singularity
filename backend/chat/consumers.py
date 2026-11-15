from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from . import events

ROLE_CHANGED = 4403  # close code: the role or access changed; reconnect to be checked again


@database_sync_to_async
def _current(user_id):
    """The user's role and active flag as stored now (the scope's user can be stale)."""
    from accounts.models import User

    return User.objects.filter(pk=user_id).only("role", "is_active").first()


class EventsConsumer(AsyncJsonWebsocketConsumer):
    """Server-to-browser events for the signed-in user. Nothing the client sends is acted on."""

    async def connect(self):
        user = self.scope.get("user")
        if user is None or not user.is_authenticated:
            await self.close(code=4401)
            return
        user = await _current(user.pk)
        if user is None or not user.is_active or not user.can_read:
            await self.close(code=4401)
            return
        self.user_id = user.pk
        self.approver = user.can_approve
        self.groups_joined = [events.user_group(user.pk)] + ([events.APPROVERS] if self.approver else [])
        for g in self.groups_joined:
            await self.channel_layer.group_add(g, self.channel_name)
        await self.accept()

    async def disconnect(self, code):
        for g in getattr(self, "groups_joined", []):
            await self.channel_layer.group_discard(g, self.channel_name)
        self.groups_joined = []

    async def receive_json(self, content, **kwargs):
        return  # one-way channel

    async def _end(self):
        for g in getattr(self, "groups_joined", []):
            await self.channel_layer.group_discard(g, self.channel_name)
        self.groups_joined = []
        await self.close(code=ROLE_CHANGED)

    async def event(self, message):
        # Re-check before every delivery: a socket opened under an older role must not keep receiving its events.
        user = await _current(self.user_id)
        if user is None or not user.is_active or not user.can_read or user.can_approve != self.approver:
            await self._end()
            return
        await self.send_json(message["payload"])

    async def session_end(self, message):
        await self._end()
