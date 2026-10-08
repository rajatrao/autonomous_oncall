---
name: incident-triage
description: Assess severity, scope, impact, affected services and dependencies of a production incident from observability data.
license: Apache-2.0
metadata:
  version: "1.0"
---
# Incident triage

1. Read the incident record first; never re-derive what is already recorded.
2. Quantify impact: error ratio, latency percentile and request volume for the primary service,
   now versus the hour before. State numbers with units and windows.
3. Severity guide (adapt to the organization's definitions when provided in incident labels):
   - SEV1: most users of a critical journey cannot complete it, or data loss/security exposure.
   - SEV2: a significant fraction (roughly > 10%) of requests on a critical journey fail.
   - SEV3: partial degradation, workaround exists, or a non-critical journey is affected.
   - SEV4/5: minor or cosmetic, no user impact.
4. Blast radius: list every service whose health degraded in the same window and every
   dependency of the primary service that reports elevated errors or latency.
5. Symptoms are observations ("5xx ratio 0.38 on POST /payments"), not explanations.
6. When a data source is unavailable, record that fact; lower confidence, do not guess.
