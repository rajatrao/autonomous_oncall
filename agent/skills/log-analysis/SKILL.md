---
name: log-analysis
description: Find new or increased error signatures in logs and relate them to components, dependencies and versions.
license: Apache-2.0
metadata:
  version: "1.0"
---
# Log analysis

1. Compare error signatures (normalized messages with ids and numbers stripped) in the incident
   window against a baseline window of the same length before it.
2. A signature that is new in the incident window is the strongest log evidence; a large increase
   of an existing signature is next.
3. Note labels on the matching lines: component, dependency/upstream, version, endpoint.
4. Quote at most a few representative lines, redacting tokens, emails, card or account numbers
   and anything that looks like a credential.
5. Distinguish cause from consequence: "upstream returned 503" in the caller's logs points at the
   upstream; a stack trace inside the service's own code after a deploy points at the service.
