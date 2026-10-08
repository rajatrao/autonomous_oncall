---
name: metrics-analysis
description: Characterize an anomaly in metrics - onset, shape, concentration by label and current trend.
license: Apache-2.0
metadata:
  version: "1.0"
---
# Metrics analysis

- Onset: first sample where the signal crossed its threshold; report as an absolute timestamp.
- Shape: step change (something switched), ramp (resource exhaustion or growing load),
  intermittent spikes (flapping dependency, partial failure) - report the observed values.
- Concentration: break the error signal down by available labels (dependency, version, endpoint,
  instance). An error that is concentrated on one label value is a strong lead.
- Trend: still rising, flat, or recovering.
- Always compare to a baseline window. A number without a baseline is not evidence.
