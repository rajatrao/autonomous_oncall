---
name: safe-remediation
description: Choose and request the least risky typed mitigation through policy-controlled tools, then verify recovery.
license: Apache-2.0
metadata:
  version: "1.0"
---
# Safe remediation

1. Prefer an existing runbook procedure over improvising.
2. Prefer reversible, scoped actions: failover to an approved target, rollback to a known-good
   version, disable a single flag, open one circuit breaker. Avoid broad actions (restarting
   everything) unless the evidence points at the process itself.
3. One action at a time. After each action, verify before considering another.
4. Pass justification, evidence and confidence with every request. The policy engine uses
   confidence: low-confidence requests are routed to a human.
5. A pending_approval result is not a failure. Report it and stop; a human decides.
6. A denied result means policy forbids it. Do not try a different action to get around a denial
   unless it is genuinely a different mitigation for the same cause; explain in the report.
7. Verification: the triggering signal must return below its threshold for the observation
   window and no new error signature may appear. If verification fails, the hypothesis was likely
   wrong or incomplete: report it so investigation resumes.
8. Duplicate requests are safe: requesting the same action twice returns the existing action.
