# Integration examples

Snippets for wiring common tools to the on-call gateway. The provider code itself lives in
`src/oncall_integrations/`; the contracts are documented in [docs/integrations.md](../docs/integrations.md).

| Path | What |
| --- | --- |
| `alertmanager/receiver.yaml` | Alertmanager receiver that posts to the gateway's Alertmanager adapter |
| `events/example-created.json` | A minimal `incident.created` event for `POST /v1/events` |

Send the example event:

```bash
curl -X POST http://localhost:7100/v1/events \
  -H "Authorization: Bearer $ONCALL_INGEST_TOKEN" -H 'content-type: application/json' \
  -d @integrations/events/example-created.json
```
