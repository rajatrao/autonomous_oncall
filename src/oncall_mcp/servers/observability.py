"""observability MCP server: metrics, logs, traces and health behind vendor-neutral tools.
The configured providers (Prometheus, Loki, mock, ...) are invisible to the agent."""
from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..common import READ, Context, tool_errors


def build(ctx: Context) -> MCPServer:
    mcp = MCPServer(name="oncall-observability", version="1.0",
                    instructions="Read-only observability. Every result includes the window; compare against baselines.")
    obs = lambda: ctx.provider("observability")  # noqa: E731
    logs = lambda: ctx.provider("logs")  # noqa: E731
    traces = lambda: ctx.provider("traces")  # noqa: E731

    @mcp.tool(annotations=READ)
    @tool_errors
    def get_service_health(service: str, window_minutes: int = Field(30, ge=1, le=1440)) -> dict:
        """Health summary for a service: error rate, request rate and latency, each with current, peak,
        baseline, onset and shape (normal, sustained, intermittent, recovered), plus error rate by version."""
        return obs().get_service_health(service, window_minutes)

    @mcp.tool(annotations=READ)
    @tool_errors
    def get_dependency_health(service: str, window_minutes: int = Field(30, ge=1, le=1440)) -> dict:
        """Health of each downstream dependency as seen from `service`: error rate summary (with shape and
        onset), latency and a status (healthy, degraded, critical). Sorted worst first."""
        return obs().get_dependency_health(service, window_minutes)

    @mcp.tool(annotations=READ)
    @tool_errors
    def query_metrics(service: str, signal: str = "error_rate", window_minutes: int = Field(30, ge=1, le=1440),
                      step_seconds: int = Field(30, ge=5, le=3600), dependency: str | None = None,
                      raw_query: str | None = None) -> dict:
        """Time series for a named signal (error_rate, request_rate, latency_p95_ms, dependency_error_rate,
        dependency_latency_p95_ms, error_rate_by_version, or any signal the operator configured) for a service.
        raw_query runs a provider-native query (e.g. PromQL) when the operator allows it."""
        if raw_query:
            if not ctx.config["integrations"]["observability"].get("allow_raw_queries", False):
                return {"ok": False, "error": "raw queries are disabled by configuration; use a named signal"}
            return obs().query_metrics(raw_query, window_minutes, step_seconds)
        return obs().query_named_signal(signal, service, window_minutes, step_seconds, dependency)

    @mcp.tool(annotations=READ)
    @tool_errors
    def query_logs(service: str, contains: str | None = None, level: str | None = None,
                   window_minutes: int = Field(30, ge=1, le=1440), limit: int = Field(30, ge=1, le=200)) -> dict:
        """Recent log lines for a service (newest first), optionally filtered by substring and level.
        Sensitive values are redacted."""
        return logs().query_logs(service, contains, level, window_minutes, limit)

    @mcp.tool(annotations=READ)
    @tool_errors
    def get_error_signatures(service: str, window_minutes: int = Field(15, ge=1, le=720)) -> dict:
        """Group error logs into normalized signatures and compare the last window with the previous one.
        New signatures (baseline_count 0) and large increases are the strongest log evidence."""
        return logs().get_error_signatures(service, window_minutes)

    @mcp.tool(annotations=READ)
    @tool_errors
    def query_traces(service: str, window_minutes: int = Field(30, ge=1, le=1440), min_duration_ms: int | None = None,
                     error_only: bool = True, limit: int = Field(20, ge=1, le=100)) -> dict:
        """Sample traces for a service. Returns unavailable when no tracing backend is configured."""
        return traces().query_traces(service, window_minutes, min_duration_ms, error_only, limit)

    return mcp
