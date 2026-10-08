---
id: database-latency
title: Payments database latency
services: [payment-api, payments-db]
tags: [database, latency, postgres, saturation, connections]
---
## Symptoms
- p95 latency of payment-api rises; `payments-db` dependency latency rises; timeouts on writes.

## Diagnosis
- Check connection counts, slow queries and recent schema migrations.
- Correlate with traffic: is it load or a single slow query?

## Mitigation
- payments-db is a protected service: no automatic action. Page the database on-call.
- If load-driven, `scale_service(payment-api)` is not a fix; consider shedding non-critical work.

## Verification
- p95 latency back to baseline; no write timeouts for 10 minutes.
