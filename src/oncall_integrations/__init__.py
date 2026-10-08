"""Pluggable integrations behind vendor-neutral interfaces.

Each capability (observability, logs, traces, changes, remediation execution,
communications, knowledge) has an abstract interface in `base.py` and one module
per provider. `registry.build(kind, cfg)` instantiates the provider named in the
customer's configuration. Adding Datadog, CloudWatch, Teams, ArgoCD... means adding
a module and a registry entry; the agent and MCP tool contracts do not change.
"""
