---
name: root-cause-analysis
description: Turn investigation findings into ranked, evidence-backed root cause hypotheses with calibrated confidence.
license: Apache-2.0
metadata:
  version: "1.0"
---
# Root cause analysis

For each candidate cause, check it against three tests:

- Timing: did the cause start at or just before the symptom? A change after the symptom began
  cannot be its cause.
- Mechanism: is there a plausible path from the cause to the observed symptom (an error
  signature mentioning the component, errors concentrated on one dependency label, one version)?
- Specificity: does the cause explain why this service/endpoint/dependency and not others?

Confidence calibration:

| Evidence | Confidence |
| --- | --- |
| Two or more independent sources agree (for example metrics by label + new log signature + change timing) and nothing contradicts | 0.85 - 0.95 |
| One strong source, others neutral | 0.6 - 0.8 |
| Correlation in time only | 0.3 - 0.5 |
| Speculation | below 0.3, keep as an alternative only |

Rules:

- Confidence is never certainty. A cause is confirmed only when acting on it restores service.
- Always keep at least one alternative hypothesis unless the evidence is overwhelming, and say
  what observation would distinguish them.
- Intermittent symptoms (alternating high and low error rates) point to flapping dependencies,
  partial capacity loss, retries or load-dependent behaviour; investigate the pattern instead of
  treating each spike as new.
- Insufficient evidence is a valid result: list the missing observations as next steps.
