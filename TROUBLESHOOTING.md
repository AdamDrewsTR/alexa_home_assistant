# Troubleshooting and recovery

For HA preparation, exactly where to set each URL, required home Wi-Fi and
background-location settings, and cellular testing, see
[Phone location setup](PHONE_LOCATION.md#what-goes-where). This guide covers
deployment failures and recovery.

## Before changing anything

Preserve `.setup`, Terraform state and backups, HA configuration backups, and
the connector's persistent data. They can contain credentials: keep them private,
out of source control, and out of screenshots/support messages.

Do not begin by deleting HA integrations, removing phone registrations,
rotating secrets, relinking skills, or destroying AWS resources. Establish which
layer failed first:

| Layer | Evidence to check |
|---|---|
| AWS identity/state | Correct profile, account ID, region, and saved Terraform state |
| Connector connection | Add-on Logs tab and connection status |
| Alexa linking | Cloud grants fetched over a working connector connection |
| Local Alexa integration | HA configuration check, HA Core logs, native endpoint status |
| Phone delivery | Cloud enrollment, accepted-location counter, actual tracker state |

## Connector cannot connect

1. Check the **saved add-on options**, not just Terraform output. Terraform
   does not update an installed add-on's options automatically.
2. The WebSocket address must be `wss://<api-id>.execute-api.<region>.amazonaws.com/connector`.
   Copy it without leading/trailing whitespace or a final slash. Newer connector
   code normalizes surrounding whitespace and one trailing slash; older
   installations may not. The phone uses a different, HTTPS address.
3. Keep the existing connector key. Verify it belongs to the same deployment's
   `/<name>/connector-key` SSM parameter without printing the value.
4. Inspect the add-on Logs tab for authentication, DNS, TLS, or network errors.
   The older generic "Cloud connection unavailable" message does not distinguish
   these causes. Check HA's outbound DNS and HTTPS connectivity.
5. After correcting saved options, restart only the connector add-on. An HA Core
   restart is not needed just for a WSS URL change.

Cloud-side checks include gateway routes/integrations, stage deployment, Lambda
invocation permissions, Lambda's `SOCKET_ENDPOINT`, and the IAM
`execute-api:ManageConnections` resource. All must reference the same WebSocket
API. Prefer a reviewed Terraform plan to repair drift rather than creating a
replacement gateway.

**Only one connector is active per stack.** A diagnostic connection using the
real key supersedes the running connector. Avoid opening test sockets while
diagnosing a live installation; doing so changes the condition being measured.

## Linked Alexa accounts disappeared from the Web UI

The panel is populated from AWS over the WebSocket. The connector starts with
an empty list and clears it on disconnect. An empty panel alone does not prove
the skills were unlinked or HA storage was erased.

Restore connectivity first. If it remains empty, verify the AWS account, region,
table, and unexpired grants. A fresh database in another account has none of the
original grants. Do not relink until the OAuth secret and the skill's account-
linking URLs are confirmed consistent with the intended deployment.

Reapplying managed HA configuration does not delete DynamoDB grants or HA's
`.storage`. It can change managed YAML and event credentials, so inspect its
preview and backups instead of assuming every local setting is unchanged.

## Wrong AWS account, changed URLs, or duplicate gateways

An AWS profile is a credential label, not a Terraform state selector. Changing
`AWS_PROFILE` while reusing another account's state can lead to creation of a
second deployment.

Before any apply, use read-only checks, substituting your actual profile:

```sh
aws sts get-caller-identity --profile home-assistant
aws apigatewayv2 get-apis --profile home-assistant --region us-east-1
terraform output relay
```

`terraform output` reads saved state; it does not prove that the profile points
to the same account. Compare the account IDs independently. Keep using
`.setup/relay.tfvars.json` with the expected `aws_account_id` guard, and keep
separate state/setup files for independent deployments.

If IDs unexpectedly changed, stop applying. Preserve both current and backup
states before further operations overwrite backups. Inventory the original
resources before importing anything. Import/reconcile the intended gateways and
their routes, integrations, and stages; do not blindly copy an old state over a
new one or destroy a stack to recover a URL.

Generated AWS gateway URLs remain stable while their APIs are retained. Lambda
code updates do not inherently change those URLs. The repository's
`prevent_destroy` guards protect planned table/API replacement, not wrong-account
deployments, manual deletion, or missing state.

## Missing secrets or authentication failures

These credentials serve different purposes:

| Credential | Used for | Not used for |
|---|---|---|
| Connector key in add-on options and SSM | Connector WebSocket authentication | Proactive Alexa events |
| OAuth secret in SSM and Alexa skill configuration | Alexa account-linking token exchange | Connector WebSocket authentication |
| Event client ID/secret in add-on options and HA YAML | HA proactive events to Alexa | Connector WebSocket authentication |

Terraform references SSM parameters but does not provision these secrets.
The wizard creates/reuses them separately. `ParameterNotFound` can indicate the
wrong account/region/path, not a need to generate a replacement.

Recover the existing credential from an authorized private backup when possible.
Do not rotate one side independently of its consumers. `install-ha` replaces
add-on options using `.setup/connector.json`; stale or blank saved values can
overwrite working options. Changing only the WSS address does not require
changing event credentials or the connector key.

## Updated code, but feature still unavailable

AWS Lambda, the installed connector add-on, and the custom integration loaded
inside HA are separate components:

- Terraform deploys cloud changes; review the plan in the original account/state.
- Update the installed add-on through HA's app store. `install-ha` is not an
  add-on upgrade command.
- When a release changes the local HA integration, preview/apply managed
  configuration, validate it, and restart HA Core to load it.

Do not use a configuration reapply to fix a malformed WSS address. Do not assume
a source checkout change is installed in HA.

If Terraform says "no changes" but observed Lambda behavior is stale, compare
the actual deployed Lambda `CodeSha256` with the intended ZIP hash. A no-change
plan alone is not proof of running-code identity. Check account, region,
function, and artifact before making a code update.

## HA configuration check failed

Do not restart HA Core after a failed check. The connector shows the check
failure on its result page and in the add-on Logs tab; HA Core logs may provide
additional detail. Correct the cause and retry the check without applying the
same files again.

Existing managed files are backed up as `.alexa-backup-<timestamp>`. Compare
the relevant backup with the current file before restoring it, preserving
unrelated changes. A new preview is required if configuration changed after
the previous preview.

For an `alexa_devices` "does not support YAML" error, follow the
[upgrade instructions](README.md#upgrading-an-existing-ha-os-installation)
for the migration to `alexa_device_endpoints`; do not delete unrelated Alexa
integrations.

## Alexa links, but devices or commands fail

OAuth success does not prove the local Alexa endpoints loaded. Check **Native
device endpoints active**, selected entities, HA validation/startup, connector
errors, and the correct skill Lambda endpoint. Relay and connector protocol
versions must be compatible.

Unsupported or conflicting capabilities appear in the dashboard's compatibility
details. Missing Alexa support for a sensor is not repaired by relinking.
Relinking and removal of old individual Alexa devices are needed for the
documented endpoint migration, not for every update or connection interruption.
Check routines after intentional rediscovery/migration.

## Safe verification and support details

Verify one layer at a time: cloud identity, connector connection, grant list,
Alexa command, local phone GPS fix, requested cellular location, then automatic
locked-phone departure/arrival. HTTP request counts do not identify location
messages, successful responses alone do not prove fresh GPS data, and CloudWatch
metrics can arrive late.

For an unresolved issue, collect app/integration versions, the exact error and
time, the affected layer, and whether it works locally. Redact tokens, secrets,
webhook paths, coordinates, and personal data. Do not enable AWS request-body
logging or post full Terraform state, add-on options, or `.setup` files.
