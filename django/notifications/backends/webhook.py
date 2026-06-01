import logging

import requests

from notifications.backends.base import BaseNotificationBackend

logger = logging.getLogger("notifications.webhook")


class WebhookBackend(BaseNotificationBackend):
    def send(self, channel, message_text, extra_params=None):
        cfg = channel.config
        url = cfg.get("url", "")
        if not url:
            logger.warning("Webhook channel %s missing url", channel.name)
            return False
        try:
            method = cfg.get("method", "POST").upper()
            headers = cfg.get("headers", {})
            if not isinstance(headers, dict):
                headers = {}
            timeout = cfg.get("timeout", 10)
            payload = {"text": message_text}
            if extra_params:
                payload.update(extra_params)
            if method == "POST":
                resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
            elif method == "PUT":
                resp = requests.put(url, json=payload, headers=headers, timeout=timeout)
            else:
                resp = requests.request(method, url, json=payload, headers=headers, timeout=timeout)
            if 200 <= resp.status_code < 300:
                return True
            logger.warning("Webhook channel %s returned %s: %s", channel.name, resp.status_code, resp.text[:200])
            return False
        except requests.RequestException as e:
            logger.warning("Webhook request error for channel %s: %s", channel.name, e)
            return False

    def send_with_reply_markup(self, channel, message_text, reply_markup, extra_params=None):
        return self.send(channel, message_text, extra_params)

    def send_with_photo(self, channel, message_text, photo_bytes):
        import base64

        return self.send(
            channel,
            message_text,
            extra_params={"photo_base64": base64.b64encode(photo_bytes).decode()},
        )
