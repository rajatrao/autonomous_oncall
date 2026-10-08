---
id: payment-api-rollback
title: payment-api bad deployment - roll back
services: [payment-api]
tags: [deployment, rollback, regression, version, release]
---
## Symptoms
- Errors start within minutes of a payment-api deployment.
- New error signatures that did not exist before (for example a TypeError in application code).
- Error rate concentrated on the new version label; dependencies healthy.

## Mitigation
- `rollback_deployment(service=payment-api, target_version=<previous healthy version>)`.
  The platform lists recent versions in `list_remediation_capabilities`.
- Policy: rollback requires human approval unless configured otherwise.

## Verification
- Error rate back under threshold on the previous version; the new signature stops.

## Follow-up
- Block the release, add a regression test for the failing input, review canary coverage.
