---
name: rca-generation
description: Maintain a living root cause analysis document during an incident and finalize it after recovery.
license: Apache-2.0
metadata:
  version: "1.0"
---
# RCA generation

Sections and what goes in them:

- Incident Summary: two or three sentences, what happened and the outcome.
- Impact: duration, affected users/requests, error ratios, business impact if known.
- Timeline: detection, key findings, decisions, actions and recovery with timestamps.
- Detection: how it was detected, and how long after onset.
- Symptoms: what users and monitoring observed.
- Root Cause: the cause, with confidence and whether it was confirmed by recovery.
- Evidence: bullet list of the observations supporting the root cause.
- Contributing Factors: conditions that made it worse or longer (no runbook, slow detection...).
- Short-Term Remediation: what fixed today's incident.
- Long-Term Strategy: engineering change that prevents recurrence.
- Process-Wide Improvements: organizational, process or operational changes, each tied to
  evidence from this incident.
- Action Items: concrete, ownable items (owner role, not a person's name).
- Resolution: final state and verification numbers.

Update sections as soon as their facts exist. Mark unconfirmed causes as hypotheses.
