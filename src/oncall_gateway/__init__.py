"""Incident gateway: durable, asynchronous bridge between alert sources and the agent.

event source (webhook | queue) -> validate -> persist (idempotent) -> durable task -> worker -> runner
"""
