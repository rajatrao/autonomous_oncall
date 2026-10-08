"""Prometheus metrics for the gateway (scraped from GET /metrics)."""
from prometheus_client import Counter, Gauge, Histogram

EVENTS = Counter("oncall_events_total", "Incident events received", ["source", "result"])
TASKS = Counter("oncall_agent_tasks_total", "Agent task outcomes", ["outcome"])
RUN_SECONDS = Histogram("oncall_agent_run_seconds", "Agent run duration",
                        buckets=(5, 15, 30, 60, 120, 300, 600, 1200))
RUNNING = Gauge("oncall_agent_runs_in_progress", "Agent runs in progress")
APPROVALS = Counter("oncall_remediation_decisions_total", "Human approval decisions", ["decision"])
OPEN = Gauge("oncall_open_incidents", "Open incidents")
