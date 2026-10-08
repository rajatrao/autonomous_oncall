"""Communications providers: Slack, generic webhook, log-only."""
from __future__ import annotations

import logging

import httpx

from .base import CommunicationsProvider, IntegrationUnavailable

log = logging.getLogger(__name__)


class LogCommunications(CommunicationsProvider):
    """No external chat: updates are only recorded in incident state and the process log."""

    def __init__(self, cfg: dict | None = None):
        pass

    def create_channel(self, name, topic):
        return {"channel": name, "delivered": False, "note": "log provider: no chat channel"}

    def post(self, channel, text, kind):
        log.info("[%s] %s: %s", channel, kind, text)
        return {"delivered": False, "note": "log provider: recorded only"}

    def set_topic(self, channel, topic):
        return {"delivered": False}


class SlackCommunications(CommunicationsProvider):
    """Slack Web API (conversations.create, chat.postMessage, conversations.setTopic).

    Config: token (bot token with channels:manage and chat:write, scoped to this workspace),
    api_url (default https://slack.com/api; the demo points this at its Slack simulator),
    fallback_channel (used when channel creation is not permitted).
    """

    def __init__(self, cfg: dict):
        self.api = cfg.get("api_url", "https://slack.com/api").rstrip("/")
        self.fallback = cfg.get("fallback_channel")
        self.client = httpx.Client(timeout=cfg.get("timeout_seconds", 5),
                                   headers={"Authorization": f"Bearer {cfg.get('token', '')}"})

    def _call(self, method: str, payload: dict) -> dict:
        try:
            r = self.client.post(f"{self.api}/{method}", json=payload)
            r.raise_for_status()
            body = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise IntegrationUnavailable(f"slack unavailable: {exc}") from exc
        if not body.get("ok"):
            raise IntegrationUnavailable(f"slack {method} failed: {body.get('error')}")
        return body

    def create_channel(self, name, topic):
        try:
            body = self._call("conversations.create", {"name": name})
            channel = body["channel"]["id"]
        except IntegrationUnavailable as exc:
            if "name_taken" in str(exc):
                return {"channel": name, "delivered": True, "existing": True}
            if self.fallback:
                return {"channel": self.fallback, "delivered": True, "fallback": True}
            raise
        self._call("conversations.setTopic", {"channel": channel, "topic": topic[:250]})
        return {"channel": channel, "delivered": True}

    def post(self, channel, text, kind):
        body = self._call("chat.postMessage", {"channel": channel or self.fallback, "text": text,
                                               "metadata": {"event_type": f"incident_{kind}", "event_payload": {}}})
        return {"delivered": True, "ts": body.get("ts")}

    def set_topic(self, channel, topic):
        self._call("conversations.setTopic", {"channel": channel, "topic": topic[:250]})
        return {"delivered": True}


class WebhookCommunications(CommunicationsProvider):
    """POST every update as JSON to a URL (Teams/Mattermost/Discord adapters, custom bots)."""

    def __init__(self, cfg: dict):
        self.url = cfg["url"]
        headers = {"Authorization": f"Bearer {cfg['token']}"} if cfg.get("token") else {}
        self.client = httpx.Client(timeout=cfg.get("timeout_seconds", 5), headers=headers)

    def _send(self, payload):
        try:
            self.client.post(self.url, json=payload).raise_for_status()
        except httpx.HTTPError as exc:
            raise IntegrationUnavailable(f"webhook unavailable: {exc}") from exc

    def create_channel(self, name, topic):
        self._send({"type": "channel", "channel": name, "topic": topic})
        return {"channel": name, "delivered": True}

    def post(self, channel, text, kind):
        self._send({"type": "update", "channel": channel, "kind": kind, "text": text})
        return {"delivered": True}

    def set_topic(self, channel, topic):
        self._send({"type": "topic", "channel": channel, "topic": topic})
        return {"delivered": True}
