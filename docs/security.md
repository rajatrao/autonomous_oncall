# Security model

The design goal: an LLM that is wrong, confused or prompt-injected (for example by a log line it
reads) still cannot do anything the customer's policy does not allow. Safety is enforced in code
outside the model, in layers.

## Layers

1. **No shell, no filesystem, no generic HTTP.** No agent has a `shell`, `filesystem`, `script`,
   `api` or `fetch` toolset. `permissions.deny` additionally denies `shell`, `run_background_job`,
   `write_file` and `edit_file`, and `runtime.safety: restricted` makes Docker Agent deny any tool
   call that is not explicitly allowed instead of prompting. The runners also pass
   `--safety restricted` / `safety_policy: restricted`.
2. **Tool whitelists per agent.** Every MCP toolset lists its `tools:` explicitly. Only the
   `remediation` agent can see production-changing tools; investigators get read-only slices.
   Tests (`tests/test_safety.py`) fail the build if this regresses.
3. **Typed operations only.** The remediation MCP server exposes six operations with typed
   parameters. There is no "run command". Parameters containing shell metacharacters are rejected.
4. **Policy engine at the boundary.** `RemediationPolicy` (`src/oncall_core/policy.py`) runs inside
   the remediation server, not in the prompt. In order it checks: known action, parameter types,
   autonomy mode (read-only denies all), category disabled, `allowed_services`, approved failover
   targets, scale bounds, feature-flag allowlist, per-incident action budget, supervised mode,
   protected services, minimum confidence, and the category's setting
   (`automatic` | `approval_required` | `disabled`). Outcomes: allow, require_approval, deny.
5. **Idempotent, compare-and-set execution.** Each request gets an idempotency key
   (`incident:action:hash(params)`); duplicates return the existing action. Execution moves
   `approved → executing` atomically, so an action runs at most once even with concurrent runs.
   The key is passed to the executor as an `Idempotency-Key` header.
6. **Human approval is out of band.** Approvals go through the gateway API with a separate approver
   token. The agent can request an action but cannot approve one.
7. **Audit.** Every request, decision, approval, execution and status change is written to the
   `audit` table and the incident timeline with the actor.

8. **Decision models are advisory or tighten-only.** An optional decision model
   ([decisions.md](decisions.md)) can reorder runbook search results, relabel a diagnosis category,
   reject a weakly evidenced root cause, or send an automatic action to a human. It cannot approve,
   execute, or loosen the policy, and its answers are validated enum values. It is off by default and
   sends incident context only to the URL you configure, so data leaves your network only if you
   point it at a hosted endpoint.

## Credentials

- **Nothing secret is in the agent file or prompts.** The OCI artifact contains only
  `${env.ONCALL_*_MCP_URL}` / `${env.ONCALL_*_MCP_TOKEN}` references; tests check for secret markers.
- **One token per MCP server.** A leaked observability token cannot change production.
- **Backend credentials live only in the server that needs them**: the Prometheus/Loki credentials in
  observability, the deploy platform's *read* token in changes, its *ops* token in remediation, the
  Slack bot token in communications. In the demo, platform-control even has separate read, ops and
  deploy tokens.
- **Scope backend credentials narrowly**: a Slack bot with `channels:manage` and `chat:write` only;
  a Kubernetes service account that can patch and scale deployments in one namespace, nothing else;
  a GitHub token with read access to deployments.
- **Configuration uses `${VAR}` references** expanded at start-up; keep values in your secret store.
  The demo generates random tokens into a git-ignored `.env`.
- **Logs are redacted** before reaching the model (tokens, card-like numbers, emails), and agents
  that read logs set `redact_secrets: true`.

## Network

- The gateway, MCP servers and Docker Agent API all require bearer tokens. Docker Agent's API is
  started with `--auth-token`.
- MCP servers enable DNS-rebinding protection by default; set `ONCALL_MCP_ALLOWED_HOST=*` only on a
  private network (the demo does, inside its compose network).
- The agent file sets `allow_private_ips: true` on its MCP servers because they run on private
  addresses next to the agent. Do not expose MCP servers publicly.

## Prompt injection

Logs, alert text and runbooks are untrusted input. The design assumes the model can be manipulated
and limits the blast radius: the worst a manipulated agent can do is request a typed action that the
policy allows for that service and confidence, which is exactly what the customer approved in
configuration. Use `supervised` or `approval_required` until you trust the setup.

## Known limitations

- Approvals are authenticated with a shared approver token; integrate your SSO/chat-ops identity for
  per-person authorization.
- The scripted model is for tests and demos only; it must never be configured for production.
- `KubernetesExecutor` supports restart and scale only and is marked experimental.
- The MCP servers do not yet rate-limit callers.
