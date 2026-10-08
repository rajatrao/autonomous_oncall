---
name: deployment-analysis
description: Decide whether a recent deployment, configuration or feature flag change is a likely cause of an incident.
license: Apache-2.0
metadata:
  version: "1.0"
---
# Change analysis

A change is a likely cause when all of these hold:

1. It happened shortly before the symptom onset (typically minutes, up to the deploy duration).
2. The affected service or one of its dependencies is the one that changed.
3. The symptom is new behaviour (a new error signature, a new failing endpoint) rather than a
   pre-existing pattern that merely continued.

Evidence against a change being causal: the symptom started before the change; the errors point
at an external dependency that did not change; the same symptom appears on versions that did not
change.

When a deployment is likely causal, the safe mitigation is a rollback to the last version that was
healthy, not a forward fix during the incident.
