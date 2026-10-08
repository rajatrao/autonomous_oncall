"""Event sources. The webhook is built into the HTTP app; queues implement EventSource.

The product never requires a queue. `ingestion.queue.provider: redis_streams` is one optional
implementation (used by the reference demo); others (SQS, Pub/Sub, Kafka) implement the same
three-step contract: receive -> Ingestor.handle (durable) -> acknowledge.
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
from abc import ABC, abstractmethod

from oncall_core.store import StoreUnavailable

from .ingest import Ingestor

log = logging.getLogger(__name__)


class EventSource(ABC):
    name = "source"

    @abstractmethod
    async def run(self, ingestor: Ingestor, stop: asyncio.Event) -> None:
        """Consume until `stop` is set. Acknowledge a message only after ingestor.handle returned."""


class RedisStreamsSource(EventSource):
    """Consumer group on a Redis stream. Each entry has a field `event` holding the JSON event.

    Delivery is at-least-once: entries are XACKed after they are durably stored; entries left
    pending by a crashed consumer are reclaimed with XAUTOCLAIM. Duplicates are harmless because
    ingestion is idempotent on event_id / idempotency_key.
    """
    name = "redis_streams"

    def __init__(self, cfg: dict):
        import redis.asyncio as redis

        self.redis = redis.from_url(cfg.get("url", "redis://localhost:6379/0"), decode_responses=True)
        self.stream = cfg.get("stream", "oncall:events")
        self.group = cfg.get("group", "oncall-gateway")
        self.consumer = cfg.get("consumer") or socket.gethostname()
        self.dead_stream = self.stream + ":rejected"

    async def _ensure_group(self) -> None:
        try:
            await self.redis.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except Exception as exc:  # BUSYGROUP: already exists
            if "BUSYGROUP" not in str(exc):
                raise

    async def _process(self, ingestor: Ingestor, entry_id: str, fields: dict) -> bool:
        try:
            raw = json.loads(fields.get("event", ""))
            if not isinstance(raw, dict):
                raise ValueError("event is not an object")
        except ValueError as exc:
            await self.redis.xadd(self.dead_stream, {"entry": entry_id, "error": str(exc), "raw": fields.get("event", "")[:10000]})
            return True
        try:
            out = await asyncio.to_thread(ingestor.handle, raw, "redis_streams")
        except StoreUnavailable as exc:
            log.warning("state store unavailable, leaving %s unacknowledged: %s", entry_id, exc)
            return False
        if out.status == "rejected":
            await self.redis.xadd(self.dead_stream, {"entry": entry_id, "error": json.dumps(out.error), "raw": json.dumps(raw)[:10000]})
        return True

    async def run(self, ingestor: Ingestor, stop: asyncio.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                await self._ensure_group()
                # Reclaim entries another (crashed) consumer never acknowledged.
                _, claimed, *_ = await self.redis.xautoclaim(self.stream, self.group, self.consumer, min_idle_time=60000, count=50)
                # Our own pending entries first (id 0), then new ones (>).
                pending = await self.redis.xreadgroup(self.group, self.consumer, {self.stream: "0"}, count=50)
                batch = list(claimed) + [e for _, entries in pending for e in entries]
                if not batch:
                    fresh = await self.redis.xreadgroup(self.group, self.consumer, {self.stream: ">"}, count=50, block=2000)
                    batch = [e for _, entries in fresh for e in entries]
                for entry_id, fields in batch:
                    if await self._process(ingestor, entry_id, fields or {}):
                        await self.redis.xack(self.stream, self.group, entry_id)
                    else:
                        await asyncio.sleep(backoff)
                        break
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("redis source error (retrying in %.0fs): %s", backoff, exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)


def build_source(cfg: dict) -> EventSource | None:
    provider = (cfg or {}).get("provider", "none")
    if provider == "none":
        return None
    if provider == "redis_streams":
        return RedisStreamsSource(cfg)
    raise ValueError(f"unknown queue provider {provider}")
