---
name: dependency-analysis
description: Evaluate degraded dependencies and whether isolation or failover is a safe mitigation.
license: Apache-2.0
metadata:
  version: "1.0"
---
# Dependency analysis

- Identify degraded dependencies by elevated error ratio or latency relative to baseline,
  as seen from the calling service (the caller's view is what users feel).
- Constant degradation suggests an outage; alternating good/bad windows suggest flapping,
  partial capacity loss or regional issues. Report the pattern.
- If a dependency is degraded and an approved failover target exists, evaluate failover using:
  the target's own health, whether the target has capacity for the shifted traffic, and the
  service impact of staying versus switching. Only targets the organization has approved are
  eligible; never invent a target.
- If no failover exists, consider a circuit breaker when the calling service has a graceful
  degraded mode; otherwise escalate.
