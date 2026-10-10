# Secrets

Every secret (bearer tokens, database passwords, model and integration API keys) reaches a process
as an environment variable. Where those variables come from depends on where you run, and the
modes never mix:

| Where it runs | Secrets come from | Started with |
| --- | --- | --- |
| Laptop (local development) | `.env` | `./scripts/demo.sh up` or `docker compose up` |
| Production on Docker Compose | A vault (HashiCorp Vault or OpenBao), only | `oncall-secrets compose --settings production.env up -d` |
| Production on Kubernetes | Kubernetes Secrets (`secretKeyRef`), only | Your manifests, with `ONCALL_SECRETS_SOURCE=kubernetes` |

In both production modes the gateway and the MCP servers refuse to start when a credential they
need is empty or still one of the `demo-only-...` values that `compose.yaml` uses as local
defaults. Errors name the variable or config path, never the value.

## Local development: `.env`

Nothing to set up. `./scripts/demo.sh up` generates `.env` with random tokens and `docker compose`
reads it. `.env` is git-ignored and is never used in production.

## Production on Docker Compose: a vault

`oncall-secrets` reads every secret from one KV version 2 path and runs `docker compose` with them.
`.env` is not read, and nothing is written to disk.

1. Put the secrets in the vault. Each key is the environment variable name:

   ```bash
   vault kv put secret/oncall \
     ONCALL_INGEST_TOKEN=... ONCALL_GATEWAY_API_TOKEN=... ONCALL_GATEWAY_APPROVER_TOKEN=... \
     ONCALL_AGENT_API_TOKEN=... ONCALL_INCIDENT_STATE_MCP_TOKEN=... ONCALL_OBSERVABILITY_MCP_TOKEN=... \
     ONCALL_CHANGES_MCP_TOKEN=... ONCALL_REMEDIATION_MCP_TOKEN=... ONCALL_COMMUNICATIONS_MCP_TOKEN=... \
     ONCALL_KNOWLEDGE_MCP_TOKEN=... ONCALL_DB_PASSWORD=... ONCALL_MODEL_API_KEY=...
   ```

   The ten boundary tokens above are required. Database passwords, model API keys,
   `ONCALL_JEV_API_KEY` and integration credentials (Slack, Prometheus, deploy and ops tokens) are
   needed only for what you use. Any other key whose name looks like a variable (`A_B_C`) is passed
   on too.

2. Put non-secret settings (model flavor, model names, autonomy) in a settings file, for example from
   [`config/production.env.example`](../config/production.env.example). It can be committed.

3. Install the command on the machine that runs Docker Compose (from this repository:
   `pipx install .` or `pip install .`), then log in and start:

   ```bash
   export VAULT_ADDR=https://vault.example.com
   vault login                     # or export VAULT_TOKEN=...
   oncall-secrets check --settings production.env
   oncall-secrets compose --settings production.env up -d
   ```

| Variable | Meaning |
| --- | --- |
| `VAULT_ADDR` | Vault or OpenBao address. Required. |
| `VAULT_TOKEN` | Token; when unset, `~/.vault-token` (written by `vault login`). |
| `VAULT_NAMESPACE` | Vault Enterprise or HCP namespace. |
| `VAULT_CACERT` | CA bundle for a private certificate authority. |
| `ONCALL_VAULT_PATH` | KV version 2 path, default `secret/oncall` (the first segment is the mount). Use one path per environment, for example `secret/oncall/staging`. |

`oncall-secrets` refuses to start when:

- the vault is unreachable, the login fails, or the path is missing or forbidden;
- a required secret is missing from the vault;
- a secret's name is set in your shell or in the settings file (production secrets come only from
  the vault: unset it, or move it to the vault).

`oncall-secrets check` lists each secret as `vault`, `MISSING (required)` or `not set (optional)`,
without values.

Rotation: secrets are read at start. After changing one in the vault, run the same
`oncall-secrets compose ... up -d`; compose recreates the containers whose environment changed.

## Production on Kubernetes: Kubernetes Secrets

Store the secrets in a Kubernetes Secret and give each container only its own keys with
`secretKeyRef`, the same least-privilege split as [`compose.yaml`](../compose.yaml). Set
`ONCALL_SECRETS_SOURCE=kubernetes` on the gateway and the MCP servers so they check what they
received.

```yaml
apiVersion: v1
kind: Secret
metadata: {name: oncall}
type: Opaque
stringData:
  ONCALL_INGEST_TOKEN: "..."
  ONCALL_GATEWAY_API_TOKEN: "..."
  ONCALL_GATEWAY_APPROVER_TOKEN: "..."
  ONCALL_AGENT_API_TOKEN: "..."
  ONCALL_REMEDIATION_MCP_TOKEN: "..."
  OPS_API_TOKEN: "..."
  ONCALL_MODEL_API_KEY: "..."
  # ...the other MCP tokens and the credentials your integrations use
---
# Gateway container (excerpt)
env:
  - {name: ONCALL_SECRETS_SOURCE, value: kubernetes}
  - {name: ONCALL_CONFIG, value: /config/oncall.yaml}
  - name: ONCALL_GATEWAY_API_TOKEN
    valueFrom: {secretKeyRef: {name: oncall, key: ONCALL_GATEWAY_API_TOKEN}}
  - name: ONCALL_GATEWAY_APPROVER_TOKEN
    valueFrom: {secretKeyRef: {name: oncall, key: ONCALL_GATEWAY_APPROVER_TOKEN}}
  # ...ONCALL_INGEST_TOKEN, ONCALL_AGENT_API_TOKEN, ONCALL_STATE_URL the same way
---
# Remediation MCP server container (excerpt): its own token and the ops token, nothing else
args: [remediation, --port, "7104"]
env:
  - {name: ONCALL_SECRETS_SOURCE, value: kubernetes}
  - name: ONCALL_REMEDIATION_MCP_TOKEN
    valueFrom: {secretKeyRef: {name: oncall, key: ONCALL_REMEDIATION_MCP_TOKEN}}
  - name: OPS_API_TOKEN
    valueFrom: {secretKeyRef: {name: oncall, key: OPS_API_TOKEN}}
---
# docker-agent container (excerpt): the model key and the six MCP tokens
env:
  - name: ONCALL_MODEL_API_KEY
    valueFrom: {secretKeyRef: {name: oncall, key: ONCALL_MODEL_API_KEY}}
  - name: ONCALL_REMEDIATION_MCP_TOKEN
    valueFrom: {secretKeyRef: {name: oncall, key: ONCALL_REMEDIATION_MCP_TOKEN}}
  # ...the other five MCP tokens
```

With `ONCALL_SECRETS_SOURCE=kubernetes`, a gateway or MCP server pod refuses to start (and shows
CrashLoopBackOff with the reason in its log) when a credential its config uses is empty or a
`demo-only` value. docker-agent is not ours to change, but a wrong MCP token is rejected by the MCP
server it calls, so the mistake fails loudly.

**Using a vault on Kubernetes:** sync it into a Kubernetes Secret with your cluster's integration
(Vault Secrets Operator, External Secrets Operator, or the Secrets Store CSI driver with secret
syncing). The pods then read it with `secretKeyRef` as above; no vault code runs in the cluster.

Rotation: after the Secret changes, `kubectl rollout restart deployment/<name>`.
