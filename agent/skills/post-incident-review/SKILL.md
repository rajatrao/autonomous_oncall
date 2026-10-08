---
name: post-incident-review
description: Derive evidence-based long-term and process-wide improvements from an incident without blaming people.
license: Apache-2.0
metadata:
  version: "1.0"
---
# Post-incident review

Look for gaps along the incident's path and only report those the evidence shows:

| Observation in this incident | Possible improvement |
| --- | --- |
| Detection came long after onset, or from users | missing or late alert, SLO-based alerting |
| No runbook matched | write a runbook for this failure mode |
| A single dependency failure took the service down | dependency isolation, circuit breaker, timeouts, failover |
| A bad deploy reached production | canary or progressive delivery, automated rollback on SLO burn, better tests |
| Investigation lacked data | missing metrics labels, logs or traces (insufficient observability) |
| Unclear who owned the failing component | ownership metadata and escalation paths |
| Mitigation waited on approval for a routine, reversible action | review the autonomy policy for that action class |

Frame findings as system and process improvements. Never assign blame to individuals.
