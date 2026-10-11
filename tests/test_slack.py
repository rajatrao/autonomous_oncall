"""Slack provider: incident channel creation and the optional announcement."""
import json

import httpx

from oncall_integrations.comms import SlackCommunications


def _slack(cfg, fail=()):
    calls = []

    def handler(request):
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        calls.append((method, body))
        if method in fail:
            return httpx.Response(200, json={"ok": False, "error": "not_in_channel"})
        if method == "conversations.create":
            return httpx.Response(200, json={"ok": True, "channel": {"id": "C123", "name": body["name"]}})
        return httpx.Response(200, json={"ok": True, "ts": "1.0"})

    prov = SlackCommunications({"token": "xoxb-test", **cfg})
    prov.client = httpx.Client(transport=httpx.MockTransport(handler))
    return prov, calls


def test_new_channel_is_announced():
    prov, calls = _slack({"announce_channel": "#oncall-proj"})
    assert prov.create_channel("inc-1", "[SEV2] checkout-api: errors")["channel"] == "C123"
    assert calls[-1] == ("chat.postMessage", {"channel": "#oncall-proj",
                                              "text": "Incident channel <#C123> created: [SEV2] checkout-api: errors"})


def test_no_announcement_without_announce_channel():
    prov, calls = _slack({})
    prov.create_channel("inc-1", "topic")
    assert [m for m, _ in calls] == ["conversations.create", "conversations.setTopic"]


def test_failed_announcement_does_not_fail_channel_creation():
    prov, _ = _slack({"announce_channel": "#oncall-proj"}, fail={"chat.postMessage"})
    assert prov.create_channel("inc-1", "topic") == {"channel": "C123", "delivered": True}
