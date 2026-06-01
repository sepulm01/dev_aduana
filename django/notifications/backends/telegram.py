import logging

import requests

from notifications.backends.base import BaseNotificationBackend

logger = logging.getLogger("notifications.telegram")

TELEGRAM_API = "https://api.telegram.org"


class TelegramBackend(BaseNotificationBackend):
    def _api_url(self, bot_token, method):
        return f"{TELEGRAM_API}/bot{bot_token}/{method}"

    def _get_config(self, channel):
        return {
            "bot_token": channel.config.get("bot_token", ""),
            "chat_id": channel.config.get("chat_id", ""),
            "parse_mode": channel.config.get("parse_mode", "HTML"),
        }

    def send(self, channel, message_text, extra_params=None):
        cfg = self._get_config(channel)
        if not cfg["bot_token"] or not cfg["chat_id"]:
            logger.warning("Telegram channel %s missing bot_token or chat_id", channel.name)
            return False
        try:
            payload = {
                "chat_id": cfg["chat_id"],
                "text": message_text,
                "parse_mode": cfg["parse_mode"],
            }
            if extra_params:
                payload.update(extra_params)
            url = self._api_url(cfg["bot_token"], "sendMessage")
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                return True
            logger.warning("Telegram send failed for channel %s: %s", channel.name, resp.text)
            return False
        except requests.RequestException as e:
            logger.warning("Telegram request error for channel %s: %s", channel.name, e)
            return False

    def send_with_reply_markup(self, channel, message_text, reply_markup, extra_params=None):
        cfg = self._get_config(channel)
        if not cfg["bot_token"] or not cfg["chat_id"]:
            logger.warning("Telegram channel %s missing bot_token or chat_id", channel.name)
            return False
        try:
            payload = {
                "chat_id": cfg["chat_id"],
                "text": message_text,
                "parse_mode": cfg["parse_mode"],
                "reply_markup": reply_markup,
            }
            if extra_params:
                payload.update(extra_params)
            url = self._api_url(cfg["bot_token"], "sendMessage")
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("result", {}).get("message_id")
            logger.warning("Telegram send_with_reply_markup failed for channel %s: %s", channel.name, resp.text)
            return None
        except requests.RequestException as e:
            logger.warning("Telegram request error for channel %s: %s", channel.name, e)
            return None

    def send_with_photo(self, channel, message_text, photo_bytes):
        cfg = self._get_config(channel)
        if not cfg["bot_token"] or not cfg["chat_id"]:
            logger.warning("Telegram channel %s missing bot_token or chat_id", channel.name)
            return False
        try:
            url = self._api_url(cfg["bot_token"], "sendPhoto")
            resp = requests.post(
                url,
                data={
                    "chat_id": cfg["chat_id"],
                    "caption": message_text,
                    "parse_mode": cfg["parse_mode"],
                },
                files={"photo": ("snapshot.jpg", photo_bytes, "image/jpeg")},
                timeout=15,
            )
            if resp.status_code == 200:
                return True
            logger.warning("Telegram sendPhoto failed for channel %s: %s", channel.name, resp.text)
            return False
        except requests.RequestException as e:
            logger.warning("Telegram sendPhoto error for channel %s: %s", channel.name, e)
            return False

    def get_updates(self, bot_token, offset=None):
        try:
            url = self._api_url(bot_token, "getUpdates")
            params = {"timeout": 30, "allowed_updates": ["callback_query"]}
            if offset:
                params["offset"] = offset
            resp = requests.get(url, params=params, timeout=35)
            if resp.status_code == 200:
                return resp.json().get("result", [])
            logger.warning("Telegram getUpdates failed: %s", resp.text)
        except requests.RequestException as e:
            logger.warning("Telegram getUpdates error: %s", e)
        return []

    def delete_message(self, channel, message_id):
        cfg = self._get_config(channel)
        if not cfg["bot_token"]:
            return False
        try:
            url = self._api_url(cfg["bot_token"], "deleteMessage")
            payload = {"chat_id": cfg["chat_id"], "message_id": message_id}
            resp = requests.post(url, json=payload, timeout=10)
            return resp.status_code == 200
        except requests.RequestException:
            return False
