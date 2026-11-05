"""Push change events to browsers over WebSocket (Channels). Best effort: never breaks a request."""
import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

log = logging.getLogger(__name__)

APPROVERS = "approvers"


def user_group(user_id: int) -> str:
    return f"user.{user_id}"


def publish(groups: list[str], payload: dict) -> None:
    try:
        layer = get_channel_layer()
        if layer is None:
            return
        send = async_to_sync(layer.group_send)
        for g in groups:
            send(g, {"type": "event", "payload": payload})
    except Exception:
        log.warning("could not publish event", exc_info=True)
