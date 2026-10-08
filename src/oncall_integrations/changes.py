"""Change providers: generic HTTP contract and GitHub deployments."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from .base import ChangesProvider, IntegrationUnavailable


class HttpChangesProvider(ChangesProvider):
    """Generic change-feed contract. Any CD system can implement three endpoints:

    GET {url}/v1/changes/deployments?service=&since_minutes=   -> {"items": [...]}
    GET {url}/v1/changes/config?service=&since_minutes=        -> {"items": [...]}
    GET {url}/v1/changes/feature-flags?service=&since_minutes= -> {"items": [...]}

    Item fields: at, service, kind, from, to, actor, description (extra fields are passed through).
    """

    def __init__(self, cfg: dict):
        self.url = cfg.get("url", "http://localhost:8090").rstrip("/")
        headers = {"Authorization": f"Bearer {cfg['token']}"} if cfg.get("token") else {}
        self.client = httpx.Client(timeout=cfg.get("timeout_seconds", 10), headers=headers)

    def _get(self, path: str, service: str | None, since_minutes: int) -> list[dict]:
        params = {"since_minutes": since_minutes}
        if service:
            params["service"] = service
        try:
            r = self.client.get(f"{self.url}{path}", params=params)
            r.raise_for_status()
            return r.json().get("items", [])
        except (httpx.HTTPError, ValueError) as exc:
            raise IntegrationUnavailable(f"change feed unavailable: {exc}") from exc

    def get_recent_deployments(self, service, since_minutes=120):
        return self._get("/v1/changes/deployments", service, since_minutes)

    def get_recent_config_changes(self, service, since_minutes=120):
        return self._get("/v1/changes/config", service, since_minutes)

    def get_recent_feature_flag_changes(self, service, since_minutes=120):
        return self._get("/v1/changes/feature-flags", service, since_minutes)


class GitHubChangesProvider(ChangesProvider):
    """GitHub Deployments API. Config: token, api_url, repos: {service: "owner/repo"}, environment.
    Config and feature-flag changes are not modelled by GitHub and return []."""

    def __init__(self, cfg: dict):
        self.api = cfg.get("api_url", "https://api.github.com").rstrip("/")
        self.repos = cfg.get("repos", {})
        self.environment = cfg.get("environment", "production")
        headers = {"Accept": "application/vnd.github+json"}
        if cfg.get("token"):
            headers["Authorization"] = f"Bearer {cfg['token']}"
        self.client = httpx.Client(timeout=10, headers=headers)

    def get_recent_deployments(self, service, since_minutes=120):
        since = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
        items = []
        targets = {service: self.repos[service]} if service in self.repos else ({} if service else self.repos)
        for svc, repo in targets.items():
            try:
                r = self.client.get(f"{self.api}/repos/{repo}/deployments",
                                    params={"environment": self.environment, "per_page": 20})
                r.raise_for_status()
            except httpx.HTTPError as exc:
                raise IntegrationUnavailable(f"github deployments unavailable: {exc}") from exc
            for d in r.json():
                at = datetime.fromisoformat(d["created_at"].replace("Z", "+00:00"))
                if at >= since:
                    items.append({"at": d["created_at"], "service": svc, "kind": "deployment", "to": d.get("ref"),
                                  "sha": d.get("sha"), "actor": (d.get("creator") or {}).get("login"),
                                  "description": d.get("description") or ""})
        return items

    def get_recent_config_changes(self, service, since_minutes=120):
        return []

    def get_recent_feature_flag_changes(self, service, since_minutes=120):
        return []
