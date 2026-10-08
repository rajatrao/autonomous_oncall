"""Remediation executors. They run only actions the policy engine already allowed."""
from __future__ import annotations

import logging

import httpx

from .base import IntegrationUnavailable, NotSupported, RemediationExecutor

log = logging.getLogger(__name__)


class DryRunExecutor(RemediationExecutor):
    """Records what would have happened. Safe default for new installations."""

    def __init__(self, cfg: dict | None = None):
        self.executed: list[dict] = []

    def capabilities(self, service: str) -> dict:
        return {"service": service, "operations": ["restart_service", "rollback_deployment", "scale_service",
                                                   "failover_dependency", "enable_circuit_breaker", "disable_feature_flag"],
                "dry_run": True}

    def execute(self, action: str, params: dict, idempotency_key: str) -> dict:
        self.executed.append({"action": action, "params": params, "idempotency_key": idempotency_key})
        log.info("dry-run %s %s", action, params)
        return {"status": "succeeded", "dry_run": True, "detail": f"would run {action} with {params}"}


class HttpOpsExecutor(RemediationExecutor):
    """Generic operations contract, so any platform (an internal ops API, a thin adapter over
    Argo CD / Spinnaker / a service mesh) can be plugged in:

    GET  {url}/v1/capabilities?service=X -> {"operations": [...], "deployments": [...], "dependencies": [...]}
    POST {url}/v1/actions/{action}       (JSON params, Idempotency-Key header) -> {"status": "succeeded"|"failed", ...}
    """

    def __init__(self, cfg: dict):
        self.url = cfg.get("url", "http://localhost:8090").rstrip("/")
        headers = {"Authorization": f"Bearer {cfg['token']}"} if cfg.get("token") else {}
        self.client = httpx.Client(timeout=cfg.get("timeout_seconds", 30), headers=headers)

    def capabilities(self, service: str) -> dict:
        try:
            r = self.client.get(f"{self.url}/v1/capabilities", params={"service": service})
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise IntegrationUnavailable(f"ops API unavailable: {exc}") from exc

    def execute(self, action: str, params: dict, idempotency_key: str) -> dict:
        try:
            r = self.client.post(f"{self.url}/v1/actions/{action}", json=params,
                                 headers={"Idempotency-Key": idempotency_key})
        except httpx.HTTPError as exc:
            raise IntegrationUnavailable(f"ops API unavailable: {exc}") from exc
        try:
            body = r.json()
        except ValueError:
            body = {"detail": r.text[:500]}
        if r.status_code >= 400:
            return {"status": "failed", "http_status": r.status_code, **body}
        return {"status": body.get("status", "succeeded"), **body}


class KubernetesExecutor(RemediationExecutor):
    """Minimal Kubernetes executor (restart and scale of Deployments) using the API server directly.

    Config: api_url, token (a ServiceAccount token scoped by RBAC to patch deployments and
    deployments/scale in the listed namespace only), namespace, verify (CA bundle path or false).
    Rollback, failover, circuit breakers and flags are platform specific: wire them through the
    http executor contract instead. Experimental: not exercised by the demo.
    """

    def __init__(self, cfg: dict):
        self.api = cfg.get("api_url", "https://kubernetes.default.svc").rstrip("/")
        self.ns = cfg.get("namespace", "default")
        self.client = httpx.Client(timeout=15, verify=cfg.get("verify", True),
                                   headers={"Authorization": f"Bearer {cfg.get('token', '')}"})

    def capabilities(self, service: str) -> dict:
        return {"service": service, "operations": ["restart_service", "scale_service"]}

    def execute(self, action: str, params: dict, idempotency_key: str) -> dict:
        path = f"{self.api}/apis/apps/v1/namespaces/{self.ns}/deployments/{params.get('service')}"
        try:
            if action == "restart_service":
                patch = {"spec": {"template": {"metadata": {"annotations": {
                    "kubectl.kubernetes.io/restartedAt": idempotency_key}}}}}
                r = self.client.patch(path, json=patch, headers={"Content-Type": "application/strategic-merge-patch+json"})
            elif action == "scale_service":
                r = self.client.patch(f"{path}/scale", json={"spec": {"replicas": params["replicas"]}},
                                      headers={"Content-Type": "application/merge-patch+json"})
            else:
                raise NotSupported(f"{action} is not supported by the kubernetes executor")
        except httpx.HTTPError as exc:
            raise IntegrationUnavailable(f"kubernetes API unavailable: {exc}") from exc
        if r.status_code >= 400:
            return {"status": "failed", "http_status": r.status_code, "detail": r.text[:500]}
        return {"status": "succeeded"}
