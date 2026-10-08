---
id: dependency-failover
title: Fail over a degraded upstream dependency
services: [checkout-api]
tags: [dependency, failover, upstream, 503, outage]
---
1. Confirm the dependency's error rate is elevated from the caller's view.
2. Confirm the approved alternate is healthy.
3. Fail over with the typed failover operation and watch the caller's error rate for 5 minutes.
