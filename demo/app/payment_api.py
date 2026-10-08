"""payment-api: the demo's customer-facing service.

POST /v1/payments -> fraud check (fraud-api) -> authorize with the active bank -> store in Postgres.
Runtime settings (version, active bank, circuit breakers, feature flags) come from platform-control
through Redis, which is how "deployments" and "failovers" happen without restarting containers.
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
import uuid

import httpx
import psycopg
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from common import DEP_DURATION, DEP_REQUESTS, DURATION, REQUESTS, SERVICE, log, metrics_endpoint, r

app = FastAPI(title=SERVICE)
metrics_endpoint(app)

BANKS = {b.split("=")[0]: b.split("=")[1] for b in os.environ.get("BANKS", "bank-api-a=http://bank-api-a:8000").split(",")}
FRAUD_URL = os.environ.get("FRAUD_URL", "http://fraud-api:8000")
DB_URL = os.environ.get("PAYMENTS_DB_URL", "")
CURRENCIES = ["USD", "USD", "USD", "EUR", "GBP", "JPY"]
client = httpx.Client(timeout=httpx.Timeout(2.5, connect=1.0))
_settings: dict = {}
_settings_at = 0.0
_db_local = threading.local()


class DependencyError(Exception):
    def __init__(self, dependency: str, detail: str):
        super().__init__(detail)
        self.dependency = dependency


def settings() -> dict:
    global _settings, _settings_at
    if time.time() - _settings_at > 1:
        try:
            raw = r().get(f"platform:service:{SERVICE}")
            _settings = json.loads(raw) if raw else {}
        except Exception:
            pass
        _settings_at = time.time()
    return {"version": "2.4.0", "active_bank": "bank-api-a", "circuit_breakers": [], "flags": {}, **_settings}


def call_dep(dep: str, method: str, url: str, body: dict | None = None) -> dict:
    start = time.time()
    try:
        resp = client.request(method, url, json=body)
        if resp.status_code >= 500:
            raise DependencyError(dep, f"upstream {dep} returned status {resp.status_code}")
        DEP_REQUESTS.labels(SERVICE, dep, "success").inc()
        return resp.json()
    except httpx.TimeoutException:
        DEP_REQUESTS.labels(SERVICE, dep, "error").inc()
        raise DependencyError(dep, f"upstream {dep} timed out after 2500ms") from None
    except httpx.HTTPError as exc:
        DEP_REQUESTS.labels(SERVICE, dep, "error").inc()
        raise DependencyError(dep, f"upstream {dep} connection failed: {type(exc).__name__}") from None
    except DependencyError:
        DEP_REQUESTS.labels(SERVICE, dep, "error").inc()
        raise
    finally:
        DEP_DURATION.labels(SERVICE, dep).observe(time.time() - start)


def db():
    conn = getattr(_db_local, "conn", None)
    if conn is None or conn.closed:
        conn = psycopg.connect(DB_URL, autocommit=True, connect_timeout=2)
        _db_local.conn = conn
    return conn


def _init_db() -> None:
    for _ in range(30):
        try:
            with psycopg.connect(DB_URL, autocommit=True, connect_timeout=2) as conn:
                conn.execute("CREATE TABLE IF NOT EXISTS payments (id TEXT PRIMARY KEY, amount NUMERIC, currency TEXT, "
                             "bank TEXT, created_at TIMESTAMPTZ DEFAULT now())")
            return
        except psycopg.Error:
            time.sleep(1)


if DB_URL:
    _init_db()


def store(payment_id: str, amount: float, currency: str, bank: str) -> None:
    if not DB_URL:
        return
    start = time.time()
    try:
        db().execute("INSERT INTO payments (id, amount, currency, bank) VALUES (%s, %s, %s, %s)",
                     (payment_id, amount, currency, bank))
        DEP_REQUESTS.labels(SERVICE, "payments-db", "success").inc()
    except psycopg.Error as exc:
        DEP_REQUESTS.labels(SERVICE, "payments-db", "error").inc()
        _db_local.conn = None
        raise DependencyError("payments-db", f"database write failed: {type(exc).__name__}") from None
    finally:
        DEP_DURATION.labels(SERVICE, "payments-db").observe(time.time() - start)


def normalize_currency(currency: str, version: str) -> str:
    # Version 2.5.0 ships a refactor that only handles USD: the "bad deployment" scenario.
    if version == "2.5.0" and currency != "USD":
        raise TypeError(f"unsupported currency '{currency}'")
    return currency


@app.post("/v1/payments")
def create_payment(body: dict | None = None):
    body = body or {}
    s = settings()
    version = s["version"]
    start = time.time()
    pid = str(uuid.uuid4())
    amount = float(body.get("amount", round(random.uniform(5, 500), 2)))
    currency = body.get("currency") or random.choice(CURRENCIES)
    status, out = 200, {}
    try:
        try:
            currency = normalize_currency(currency, version)
        except TypeError as exc:
            status = 500
            log("error", f"TypeError in CurrencyNormalizer.normalize: {exc}", component="CurrencyNormalizer",
                version=version, error_type="TypeError", payment_id=pid)
            return JSONResponse({"error": "internal_error"}, status_code=500)
        call_dep("fraud-api", "POST", f"{FRAUD_URL}/v1/score", {"amount": amount, "currency": currency})
        bank = s["active_bank"]
        if bank in s.get("circuit_breakers", []):
            alternates = [b for b in BANKS if b != bank and b not in s["circuit_breakers"]]
            if not alternates:
                status = 503
                log("warning", f"circuit open for {bank}; rejecting payment fast", dependency=bank, version=version)
                return JSONResponse({"error": "bank_unavailable"}, status_code=503)
            bank = alternates[0]
        call_dep(bank, "POST", f"{BANKS[bank]}/v1/authorize", {"amount": amount, "currency": currency})
        store(pid, amount, currency, bank)
        out = {"payment_id": pid, "status": "authorized", "bank": bank}
        return out
    except DependencyError as exc:
        status = 502
        log("error", str(exc), dependency=exc.dependency, version=version, payment_id=pid)
        return JSONResponse({"error": "dependency_failure", "dependency": exc.dependency}, status_code=502)
    finally:
        REQUESTS.labels(SERVICE, str(status), version, "/v1/payments").inc()
        DURATION.labels(SERVICE, "/v1/payments").observe(time.time() - start)


def _probe_standby_banks():
    """Health-probe the banks that carry no traffic so their health is observable before a failover."""
    while True:
        s = settings()
        for bank, url in BANKS.items():
            if bank == s["active_bank"]:
                continue
            try:
                call_dep(bank, "GET", f"{url}/v1/status")
            except DependencyError:
                pass
        time.sleep(3)


threading.Thread(target=_probe_standby_banks, daemon=True).start()
