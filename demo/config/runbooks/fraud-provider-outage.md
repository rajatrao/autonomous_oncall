---
id: fraud-provider-outage
title: Fraud scoring provider outage
services: [payment-api, fraud-api]
tags: [dependency, fraud, circuit-breaker, timeout, upstream]
---
## Symptoms
- payment-api errors with `upstream fraud-api` messages; fraud-api latency high or 5xx.

## Mitigation
- There is no alternate fraud provider. `enable_circuit_breaker(service=payment-api, dependency=fraud-api)`
  only with risk sign-off: it fails payments fast instead of letting them time out.
- Notify the risk team in the incident channel.

## Verification
- payment-api latency back to baseline; fraud-api error rate recovering.
