---
id: bank-provider-failover
title: Bank provider degraded - fail over to the standby bank
services: [payment-api]
tags: [dependency, bank, failover, bank-api-a, bank-api-b, upstream, authorization]
---
## Symptoms
- payment-api 5xx rate rises; error logs read `upstream bank-api-X returned status 503` or time out.
- `dependency_requests_total{outcome="error"}` rises for one bank while the other stays healthy.
- No payment-api deployment in the window.

## Diagnosis
1. Confirm which bank is active (platform capabilities: `active_dependencies.bank`).
2. Compare the active bank's error rate with the standby's health probes.
3. Rule out a payment-api deployment or config change.
4. Sustained failure: fail over now. Intermittent bursts: keep observing until the pattern is
   clear (at least two cycles), then fail over; do not open one incident per burst.

## Mitigation
- `failover_dependency(service=payment-api, dependency=<failing bank>, target=<healthy bank>)`.
  Approved targets: bank-api-a <-> bank-api-b. Policy: automatic in the demo.
- If the standby is also unhealthy: `enable_circuit_breaker` and page the payments lead.

## Verification
- payment-api 1m error rate under 1% for 2 minutes; errors for the new bank stay at baseline.

## Follow-up
- Open a ticket with the provider; fail back only after they confirm resolution.
