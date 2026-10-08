"""alert-evaluator: deterministic alerting for the demo (a tiny stand-in for Alertmanager rules).

Rule (per service in ALERT_SERVICES):
  fire    when the errors of the last 5 minutes exceed FIRE_THRESHOLD of 5 minutes of traffic (a burn-rate style rule, so short
          bursts accumulate instead of each one paging separately) and the 1-minute ratio is too
          (errors are still happening, not just remembered by the long window)
  resolve when the 1-minute error ratio stays below RESOLVE_THRESHOLD for RESOLVE_FOR seconds
The two windows and thresholds are the hysteresis that turns a flaky provider into one incident
with updates rather than a new incident per burst. Events use the vendor-neutral schema v1.0 and
are published to the gateway's queue (Redis stream) or webhook.
"""
from __future__ import annotations

import json
import os
import time

import httpx

from common import log, r

PROM = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")
SERVICES = os.environ.get("ALERT_SERVICES", "payment-api").split(",")
FIRE = float(os.environ.get("FIRE_THRESHOLD", "0.08"))
RESOLVE = float(os.environ.get("RESOLVE_THRESHOLD", "0.01"))
RESOLVE_FOR = float(os.environ.get("RESOLVE_FOR", "60"))
INTERVAL = float(os.environ.get("EVAL_INTERVAL", "10"))
UPDATE_EVERY = float(os.environ.get("UPDATE_EVERY", "120"))
SINK = os.environ.get("ALERT_SINK", "redis")  # redis | webhook
STREAM = os.environ.get("EVENT_STREAM", "oncall:events")
WEBHOOK = os.environ.get("GATEWAY_URL", "http://gateway:7100") + "/v1/events"
WEBHOOK_TOKEN = os.environ.get("GATEWAY_INGEST_TOKEN", "")
client = httpx.Client(timeout=5)

# Errors in the last 5 minutes relative to the requests 5 minutes of current traffic would carry.
# Counting errors (instead of a 5m ratio) means a freshly reset Prometheus cannot fire on seconds
# of data, and short bursts accumulate toward the threshold.
Q5 = ('sum(increase(http_server_requests_total{{service="{s}",status=~"5.."}}[5m])) / '
      'clamp_min(sum(rate(http_server_requests_total{{service="{s}"}}[1m])) * 300, 1)')
Q1 = ('sum(rate(http_server_requests_total{{service="{s}",status=~"5.."}}[1m])) / '
      'clamp_min(sum(rate(http_server_requests_total{{service="{s}"}}[1m])), 1e-9)')


def query(q: str) -> float | None:
    try:
        res = client.get(f"{PROM}/api/v1/query", params={"query": q}).json()["data"]["result"]
        return float(res[0]["value"][1]) if res else None
    except Exception:
        return None


def severity(rate: float) -> str:
    return "SEV1" if rate >= 0.5 else "SEV2" if rate >= 0.15 else "SEV3"


def publish(event: dict) -> None:
    if SINK == "webhook":
        headers = {"Authorization": f"Bearer {WEBHOOK_TOKEN}"} if WEBHOOK_TOKEN else {}
        client.post(WEBHOOK, json=event, headers=headers).raise_for_status()
    else:
        r().xadd(STREAM, {"event": json.dumps(event)})
    log("info", f"published {event['event_type']} for {event['incident']['id']}", event_id=event["event_id"])


def event(kind: str, st: dict, svc: str, rate5: float, rate1: float | None, summary: str) -> dict:
    st["seq"] = st.get("seq", 0) + 1
    return {
        "version": "1.0", "event_type": kind, "event_id": f"{st['incident_id']}-{st['seq']:03d}-{kind.split('.')[1]}",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": {"system": "demo-alert-evaluator", "url": "http://localhost:9090/alerts"},
        "incident": {"id": st["incident_id"], "severity": severity(max(rate5, rate1 or 0)), "service": svc,
                     "environment": "production", "summary": summary,
                     "labels": {"alertname": f"{svc}-error-rate", "team": "payments"}},
        "signals": [{"name": "error_rate_5m", "value": round(rate5, 4), "threshold": FIRE, "unit": "ratio"},
                    {"name": "error_rate_1m", "value": round(rate1 or 0, 4), "unit": "ratio"}],
        "links": {"dashboard": "http://localhost:3000", "metrics": "http://localhost:9090"},
    }


def evaluate(svc: str) -> None:
    key = f"alerts:{svc}"
    st = json.loads(r().get(key) or "{}")
    rate5, rate1 = query(Q5.format(s=svc)), query(Q1.format(s=svc))
    if rate5 is None:
        return
    now = time.time()
    if not st.get("firing"):
        if rate5 > FIRE and (rate1 or 0) > FIRE:  # budget burned AND errors happening now
            st = {"firing": True, "incident_id": time.strftime("INC-%Y%m%d-%H%M%S", time.gmtime()), "since": now,
                  "last_update": now, "last_rate": rate5, "below_since": None}
            publish(event("incident.created", st, svc, rate5, rate1,
                          f"{svc} error rate {rate5:.1%} over 5m (threshold {FIRE:.0%})"))
    else:
        if rate1 is not None and rate1 < RESOLVE:
            st["below_since"] = st.get("below_since") or now
            if now - st["below_since"] >= RESOLVE_FOR:
                publish(event("incident.recovery_detected", st, svc, rate5, rate1,
                              f"{svc} error rate back under {RESOLVE:.0%} for {int(RESOLVE_FOR)}s"))
                st = {"firing": False}
        else:
            st["below_since"] = None
            changed = abs(rate5 - st.get("last_rate", 0)) > max(0.05, 0.5 * st.get("last_rate", 0))
            if changed and now - st.get("last_update", 0) >= UPDATE_EVERY:
                st["last_update"], st["last_rate"] = now, rate5
                publish(event("incident.updated", st, svc, rate5, rate1,
                              f"{svc} error rate now {rate5:.1%} over 5m (1m: {(rate1 or 0):.1%})"))
    r().set(key, json.dumps(st))


def main() -> None:
    log("info", "alert evaluator started", services=SERVICES, fire=FIRE, resolve=RESOLVE)
    while True:
        for svc in SERVICES:
            try:
                evaluate(svc)
            except Exception as exc:
                log("error", f"evaluation failed: {exc}", alert_service=svc)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
