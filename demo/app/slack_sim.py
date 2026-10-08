"""slack-sim: a minimal Slack Web API simulator so the demo needs no Slack workspace.

Implements conversations.create, conversations.setTopic, chat.postMessage and conversations.list
with Slack's response shapes ({"ok": true, ...} / {"ok": false, "error": ...}). Messages are kept
in memory. POST /api/admin.outage {"down": true} simulates a Slack outage.
"""
from __future__ import annotations

import hashlib
import time

from fastapi import FastAPI, Request

from common import log, metrics_endpoint

app = FastAPI(title="slack-sim")
metrics_endpoint(app)
CHANNELS: dict[str, dict] = {}
STATE = {"down": False}


def _find(ref: str) -> dict | None:
    return CHANNELS.get(ref) or next((c for c in CHANNELS.values() if c["name"] == ref.lstrip("#")), None)


async def _body(request: Request) -> dict:
    try:
        return await request.json()
    except ValueError:
        return dict(await request.form())


@app.post("/api/{method}")
async def api(method: str, request: Request):
    body = await _body(request)
    if method == "admin.reset":
        CHANNELS.clear()
        STATE["down"] = False
        return {"ok": True}
    if method == "admin.outage":
        STATE["down"] = bool(body.get("down", True))
        return {"ok": True, "down": STATE["down"]}
    if STATE["down"]:
        return {"ok": False, "error": "service_unavailable"}
    if not request.headers.get("authorization", "").startswith("Bearer "):
        return {"ok": False, "error": "not_authed"}
    if method == "conversations.create":
        name = str(body.get("name", "")).lower()
        if _find(name):
            return {"ok": False, "error": "name_taken"}
        cid = "C" + hashlib.sha1(name.encode()).hexdigest()[:9].upper()
        CHANNELS[cid] = {"id": cid, "name": name, "topic": "", "created": time.time(), "messages": []}
        log("info", f"channel created #{name}", channel=cid)
        return {"ok": True, "channel": {"id": cid, "name": name}}
    if method == "conversations.setTopic":
        ch = _find(str(body.get("channel", "")))
        if not ch:
            return {"ok": False, "error": "channel_not_found"}
        ch["topic"] = body.get("topic", "")
        return {"ok": True, "channel": {"id": ch["id"], "topic": {"value": ch["topic"]}}}
    if method == "chat.postMessage":
        ch = _find(str(body.get("channel", "")))
        if not ch:
            return {"ok": False, "error": "channel_not_found"}
        ts = f"{time.time():.6f}"
        kind = ((body.get("metadata") or {}).get("event_type") or "").removeprefix("incident_")
        ch["messages"].append({"ts": ts, "text": body.get("text", ""), "kind": kind, "user": "oncall-agent"})
        return {"ok": True, "channel": ch["id"], "ts": ts}
    if method == "conversations.list":
        return {"ok": True, "channels": [{"id": c["id"], "name": c["name"]} for c in CHANNELS.values()]}
    return {"ok": False, "error": "unknown_method"}


@app.get("/channels")
def channels():
    """Read-only view used by the dashboard."""
    return {"down": STATE["down"],
            "channels": sorted(CHANNELS.values(), key=lambda c: c["created"], reverse=True)}
