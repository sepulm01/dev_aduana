from notifications.backends.base import BaseNotificationBackend
from notifications.backends.telegram import TelegramBackend
from notifications.backends.webhook import WebhookBackend


def get_backend(channel_type):
    backends = {
        "telegram": TelegramBackend,
        "webhook": WebhookBackend,
    }
    backend_cls = backends.get(channel_type)
    if backend_cls is None:
        raise ValueError(f"Unknown channel type: {channel_type}")
    return backend_cls()
