"""Filesystem runbooks: markdown files with YAML frontmatter (id, title, services, tags)."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from .base import KnowledgeProvider

_WORD = re.compile(r"[a-z0-9]+")


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


class FilesystemKnowledge(KnowledgeProvider):
    def __init__(self, cfg: dict):
        self.dir = Path(cfg.get("runbooks_dir", "runbooks"))
        self.history_file = cfg.get("historical_incidents_file")

    def _load(self) -> list[dict]:
        books = []
        if not self.dir.exists():
            return books
        for p in sorted(self.dir.glob("**/*.md")):
            text = p.read_text()
            meta, body = {}, text
            m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
            if m:
                meta = yaml.safe_load(m.group(1)) or {}
                body = m.group(2)
            books.append({
                "id": meta.get("id", p.stem),
                "title": meta.get("title", p.stem.replace("-", " ")),
                # YAML turns tags like 503 into numbers; keep everything as strings.
                "services": [str(s) for s in meta.get("services") or []],
                "tags": [str(s) for s in meta.get("tags") or []],
                "body": body.strip(),
            })
        return books

    def search_runbooks(self, query: str, service: str | None = None, limit: int = 5) -> list[dict]:
        q = _words(query)
        scored = []
        for b in self._load():
            hay = _words(" ".join([b["title"], " ".join(b["tags"]), " ".join(b["services"]), b["body"][:2000]]))
            score = len(q & hay) + (3 if service and service in b["services"] else 0)
            score += 2 * len(q & _words(b["title"] + " " + " ".join(b["tags"])))
            if score:
                scored.append((score, b))
        scored.sort(key=lambda x: -x[0])
        return [{"id": b["id"], "title": b["title"], "services": b["services"], "tags": b["tags"], "score": s,
                 "excerpt": b["body"][:300]} for s, b in scored[:limit]]

    def get_runbook(self, runbook_id: str) -> dict:
        for b in self._load():
            if b["id"] == runbook_id:
                return b
        raise KeyError(runbook_id)

    def historical_seed(self) -> list[dict]:
        if not self.history_file or not Path(self.history_file).exists():
            return []
        return yaml.safe_load(Path(self.history_file).read_text()) or []
