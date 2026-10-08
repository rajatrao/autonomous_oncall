"""traffic-generator: steady synthetic payment traffic."""
from __future__ import annotations

import os
import random
import threading
import time

import httpx

URL = os.environ.get("TARGET_URL", "http://payment-api:8000/v1/payments")
RPS = float(os.environ.get("RPS", "8"))
WORKERS = int(os.environ.get("WORKERS", "8"))


def worker() -> None:
    client = httpx.Client(timeout=5)
    while True:
        start = time.time()
        try:
            client.post(URL, json={"amount": round(random.uniform(5, 500), 2)})
        except httpx.HTTPError:
            pass
        time.sleep(max(0.0, WORKERS / RPS - (time.time() - start)))


if __name__ == "__main__":
    for _ in range(WORKERS):
        threading.Thread(target=worker, daemon=True).start()
    while True:
        time.sleep(3600)
