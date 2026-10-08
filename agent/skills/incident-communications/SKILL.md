---
name: incident-communications
description: Write clear, factual, non-speculative incident updates for stakeholders.
license: Apache-2.0
metadata:
  version: "1.0"
---
# Incident communications

Template:

> **[SEVx] <service> - <status>**
> Impact: <who is affected and how, with numbers>
> What we know: <current hypothesis + confidence, or "investigating">
> What we're doing: <current action>
> Next update: <condition or time>

- Initial notice within the first minutes, even if little is known.
- Update on every status change, every remediation request (including pending approval) and on
  recovery. Close with a summary linking the RCA.
- Say "likely" or "we believe" for unconfirmed causes; "confirmed" only after verified recovery.
- No secrets, credentials, personal data, or raw logs.
- If delivery fails, the update is still recorded; never block incident response on chat.
