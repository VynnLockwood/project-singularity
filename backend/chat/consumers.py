from channels.generic.websocket import AsyncJsonWebsocketConsumer

from . import events


class EventsConsumer(AsyncJsonWebsocketConsumer):
    """Server-to-browser events for the signed-in user. Nothing the client sends is acted on."""

    async def connect(self):
        user = self.scope.get("user")
        if user is None or not user.is_authenticated or not user.can_read:
            await self.close(code=4401)
            return
        self.groups_joined = [events.user_group(user.pk)] + ([events.APPROVERS] if user.can_approve else [])
        for g in self.groups_joined:
            await self.channel_layer.group_add(g, self.channel_name)
        await self.accept()

    async def disconnect(self, code):
        for g in getattr(self, "groups_joined", []):
            await self.channel_layer.group_discard(g, self.channel_name)

    async def receive_json(self, content, **kwargs):
        return  # one-way channel

    async def event(self, message):
        await self.send_json(message["payload"])
