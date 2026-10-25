# Alexa + Home Assistant

Terraform-managed AWS bridges for Home Assistant's built-in
[Alexa Smart Home](https://www.home-assistant.io/integrations/alexa.smart_home/)
and [Alexa Custom Skill](https://www.home-assistant.io/integrations/alexa.intent/)
integrations.

- **Smart Home:** "Alexa, turn on the kitchen light", discovery, state queries,
  scenes/scripts, and forwarding of the AcceptGrant used for proactive events.
- **Optional Custom skill:** "Alexa, ask home assistant what is the home status",
  with intents implemented by Home Assistant's `intent_script`.

Home Assistant remains responsible for capabilities, device filters, actions,
and speech. The bridges forward requests rather than duplicate that logic.
This does not replace Alexa with HA Assist or provide local-only voice control.

## Outbound relay setup (recommended)

**Experimental: locally tested, not yet verified against a live Alexa account
and HA OS installation.** This mode builds an outbound-only AWS connection:
no public HA URL, router forwarding, domain, or VPN changes. It is inexpensive,
not guaranteed free.

```text
Alexa -> skill Lambda -> relay Lambda -> API Gateway WebSocket -> local connector
Browser -> AWS account-linking page -> approval in local connector UI
```

API Gateway holds the WebSocket; no EC2 server, load balancer, NAT gateway, or
third-party tunnel is needed. The public HTTP API serves only account-linking
routes, not HA's frontend. The connector accepts only `/api/alexa` and
`/api/alexa/smart_home`, with a fixed local destination. A cloud deployment
compromise could still issue commands through these permitted endpoints:
protect the AWS account and limit exposed HA entities.

### One setup entry point for HA OS

Prerequisites: Python >= 3.11, Terraform >= 1.7, Node.js/npm, an AWS account with
deployment credentials configured (prefer a dedicated account/profile and
short-lived credentials), and an Amazon developer account associated with your
Echo devices. Authorize the official ASK CLI on first use:

```sh
python3 bootstrap.py --login --custom --ha-url http://homeassistant.local:8123
```

Omit `--login` on later runs; omit `--custom` for Smart Home only. Pass the same
`--name`, `--locale`, and `--custom` flags when resuming. For a non-default ASK
profile, pass `--ask-profile NAME` (also used by `--login`).
Use `--browser edge` on macOS to open HA authorization and wizard pages in
Microsoft Edge instead of the default browser. Other browser names supported by
Python's `webbrowser` module are accepted. This option does not change the
separate ASK CLI browser login performed by `--login`.

The bootstrap installs Python dependencies in `.venv` and the pinned official
ASK CLI locally when missing. The wizard:

1. Reads your Amazon vendor and checks the AWS account identity.
2. Creates connector/OAuth credentials in SSM SecureString, outside Terraform
   state, after your approval.
3. Creates private development skills without endpoints using official SMAPI
   commands, obtaining their real skill IDs without a circular dependency.
4. Shows and applies a Terraform plan for the relay and skill-scoped bridges,
   then configures skill
   endpoints, account linking, custom interaction models, proactive-event
   permission/credentials. Enable each development skill manually in the Alexa app.
5. Opens HA's native local OAuth consent page, adds this app repository through
   Supervisor, installs the connector, and configures its options.
6. Opens the HA app page. Open its Web UI to select entities, preview generated
   YAML, back up/apply it, check configuration, and explicitly approve a restart.

**Account consent cannot be skipped.** In the Alexa app, enable/link each
development skill, then compare the displayed code with the local connector's
pending request and click Approve. This also works if Alexa is on your phone
and the local connector UI is open on another device. Never approve an
unexpected code. A linked account can be revoked from the same local UI.

HA app installation from GitHub requires these files to be published to the
repository's default branch first. Setup never pushes code or publishes a
public Alexa skill. Uncertain skill-creation results stop rather than create
duplicates; inspect the Alexa console and resume with `--adopt-skill-id ID`.
An existing unrelated Alexa/intent configuration is a manual merge boundary:
setup refuses to overwrite it.

The app repository URL is read from this checkout's Git `origin`, not a
hard-coded owner account. Override it with
`--repository-url https://github.com/OWNER/REPO` when installing from another
public app repository. Never include credentials in that URL.
Deployment-specific accounts, skill IDs, endpoints, and secrets belong in the
ignored `.setup/`, Terraform state, or local variable files, not source code.

### Separate steps / HA Container

Run from the checkout; the distribution is not a standalone PyPI wheel:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
npm ci --ignore-scripts
# First use: node_modules/.bin/ask configure
.venv/bin/python wizard.py provision --custom
.venv/bin/python wizard.py configure-ha --config-dir /path/to/ha/config
.venv/bin/python wizard.py connector-config
.venv/bin/ha-alexa-connector --config .setup/connector.json --data-dir .setup/connector
```

`connector-config` uses HA's native browser login and stores refresh credentials
locally, not in AWS. The connector refreshes expired local HA access tokens.
Its approval UI is `http://127.0.0.1:8099`; keep it private. For remote access,
use your existing VPN plus a local SSH forward, not a public UI binding.
Run the connector under your service manager for automatic startup/restart.

For Docker, build with `docker build -t ha-alexa-connector addon`. On a Linux
home server, `--network host` preserves the loopback-only approval UI:

```sh
docker run --name ha-alexa-connector --restart unless-stopped --network host \
  -v "$PWD/.setup/connector.json:/data/options.json:ro" \
  -v ha-alexa-connector-data:/data \
  ha-alexa-connector
```

Host networking behavior differs on Docker Desktop; the foreground Python
connector is the simplest portable option. Do not expose the local approval UI
to your LAN or Internet. HA OS uses authenticated Supervisor ingress instead.

### What is stored / operational limits

- `.setup/` contains private setup progress and connector/HA credentials, with
  owner-only permissions on generated files. Do not share it or Terraform plans,
  state, app options, or configuration backups. AWS and ASK login credentials
  remain in their normal credential stores.
- AWS stores hashed access/refresh tokens, short-lived authorization codes and
  command responses, and connector/OAuth secrets in SSM. Relay linking does not
  send HA passwords, HA tokens, or the Supervisor token to AWS.
- The HA OS app has **administrator-level Core API access**, write access to HA
  configuration, and permission to check/restart Core. Cloud messages cannot
  invoke the local setup/restart UI, but treat the app as trusted software.
- Browser approval requests expire after five minutes, authorization codes
  after one minute, access tokens after an hour, and refresh grants after
  90 days of refresh inactivity. Active refresh renews the grant; relink after
  expiry. Revocation invalidates access and refresh
  tokens immediately. DynamoDB TTL cleanup is asynchronous; application checks
  enforce expiry even before physical deletion.
- Messages are deliberately limited to **28 KB**, below API Gateway's 32 KB
  frame limit. Large discovery results fail explicitly: reduce the entity
  allowlist. This is an Alexa command transport, not a general HTTP tunnel.
- API Gateway disconnects sockets after two hours. The connector reconnects
  with backoff and polls every ten seconds (also preventing idle timeout).
  Requests are bounded; failures are surfaced, not silently replayed. Request
  IDs are persisted locally for 24 hours to reject duplicate deliveries.
- One connector/home per stack. Connecting a replacement supersedes the old
  connection. Keep `.setup`, Terraform state, and the connector data volume for
  reliable recovery; do not run two setup processes against one stack.
- AWS charges apply to WebSocket minutes/messages, Lambda, DynamoDB, logs, and
  transfer. Configure billing alerts independently; API throttles and log
  retention are not a spending cap. No free-tier eligibility is assumed.

Run the live tests under "Verify end to end" before relying on the system.
Cloud deployment, actual HA OS Supervisor ingress, phone account-linking
behavior, and Alexa timing remain live acceptance gates.

### Removing a deployment

Stop/uninstall the local connector and disable the development skills in Alexa.
From the original checkout with its saved state, review and approve
`terraform destroy -var-file=.setup/relay.tfvars.json`. This removes the AWS
relay and its token records, but not the Alexa skills or the two SSM credentials
created by the wizard. Delete those separately in the Alexa developer console
and AWS Parameter Store when no longer needed. Restore the backed-up HA YAML
or remove the managed includes and check configuration before restarting HA.

## Direct HTTPS mode (alternative)

The remainder of this guide describes the original bring-your-own-public-URL
mode (`relay_enabled = false`), without the wizard or connector.

### Architecture and scope

```text
Alexa Smart Home skill -> scoped Lambda -> HTTPS /api/alexa/smart_home
Alexa Custom skill    -> scoped Lambda -> HTTPS /api/alexa
Alexa account linking <-> Home Assistant /auth/authorize and /auth/token
Home Assistant        -> Alexa event gateway (optional proactive state updates)
```

In direct mode, Terraform creates separate Lambda functions, execution roles, skill-scoped
invocation permissions, and log groups with retention. Python uses only its
standard library, verifies TLS, never follows redirects, and does not log
request/response bodies or account tokens. There are no long-lived HA tokens,
OAuth secrets, public Lambda URLs, or custom OAuth servers in this deployment.

You supply an existing public Home Assistant HTTPS origin on port 443 with a
trusted certificate. DNS, TLS, reverse proxies, tunnels, and Alexa skill creation
are **not** managed here. A Cloudflare Access interactive-login challenge in front
of the API/token endpoints will not work with this direct-OAuth design. A tunnel
alone is not the same as Access authentication. Do not disable an existing Access
policy just to use this project; add a separately designed authenticated proxy
flow if keeping Access is required.

## 1. Create the skills

Use the Amazon account associated with your Echo devices in the
[Alexa Developer Console](https://developer.amazon.com/alexa/console/ask).

1. Create a **Smart Home** skill, **Provision your own**, payload version **v3**.
   Record its `amzn1.ask.skill.…` ID.
2. Optionally create a separate **Custom** skill, **Provision your own**, and
   record that ID. These are two different skill types, not two endpoints on one
   skill.

Choose the AWS region based on the skill's locale, not your physical location:
`us-east-1` for US/Canada/Brazil, `eu-west-1` for European/Indian locales, and
`us-west-2` for Japan/Australia. Check the linked HA docs for your locale. This
configuration deploys one region per stack.

## 2. Deploy AWS resources

Requires Terraform >= 1.7 and AWS credentials from your normal AWS profile/SSO
or environment. Do not put AWS credentials in Terraform variables.

```sh
cp terraform.tfvars.example terraform.tfvars
# Edit the URL, skill ID(s), and region.
terraform init
terraform fmt -check -recursive
terraform validate
terraform plan -out=deployment.tfplan
terraform apply deployment.tfplan
terraform output
```

No apply is performed by tests. Review the plan before applying. Commit
`.terraform.lock.hcl`; local variable files, plans, state, and build artifacts are
ignored. Before sharing production state, configure an appropriately secured
remote backend with encryption, access controls, and locking. AWS resources may
incur charges; this project does not promise a free deployment.

Paste `smart_home_endpoint_arn` into the Smart Home skill's **Default endpoint**.
If enabled, paste `custom_endpoint_arn` into the Custom skill's **Endpoint > AWS
Lambda ARN > Default Region**. Terraform installs each required Alexa trigger
and restricts it to its own skill ID.

## 3. Configure account linking for each skill

Use `terraform output account_linking`:

| Setting | Value |
|---|---|
| Grant type | Authorization Code Grant |
| Authorization URI | HA origin + `/auth/authorize` |
| Access Token URI | HA origin + `/auth/token` |
| Client ID | Regional URL from the output; **keep its trailing slash** |
| Client Secret | An arbitrary random value entered in Alexa only; HA does not check it |
| Authentication scheme | **Credentials in request body**, not HTTP Basic |
| Scope | `smart_home` for Smart Home; `intent` for Custom |

Require account linking on both skills. Leave app-to-app linking off, and leave
Domain List and Default Access Token Expiration Time empty as in the HA docs.
The regional Client ID is an account-linking value, not an Alexa Skill Messaging
client ID.

## 4. Configure Home Assistant and custom intents

Merge [examples/home-assistant.yaml](examples/home-assistant.yaml) into your
configuration, replacing the example entities. Keep the explicit allowlist:
without a filter, HA exposes all supported entities. Check HA configuration and
restart before discovery.

For the optional Custom skill, paste
[examples/interaction-model.json](examples/interaction-model.json) into its
interaction model JSON editor for your language, then save and build. The sample
queries state only; new actionable intents should map to explicitly chosen HA
scripts/entities, not arbitrary spoken entity IDs.

To support a LaunchRequest ("Alexa, open home assistant"), add this to the
existing `intent_script` block, replacing the key with your Custom skill ID:

```yaml
amzn1.ask.skill.YOUR-CUSTOM-SKILL-ID:
  speech:
    type: plain
    text: You can ask me for the home status.
  reprompt:
    type: plain
    text: Would you like the home status?
```

HA also supports an intent named `<custom-skill-id>.SessionEndedRequest` for
session-end actions. The bridge forwards these requests but does not speak error
responses after a session has ended.

### Optional proactive state reporting

In the Smart Home skill enable **Permissions > Send Alexa Events**. Put the
resulting **Alexa Skill Messaging** client ID/secret in HA's `secrets.yaml`,
and uncomment the `endpoint`, `client_id`, and `client_secret` example settings.
Choose the gateway for the skill region:

| AWS region | Event gateway |
|---|---|
| `us-east-1` | `https://api.amazonalexa.com/v3/events` |
| `eu-west-1` | `https://api.eu.amazonalexa.com/v3/events` |
| `us-west-2` | `https://api.fe.amazonalexa.com/v3/events` |

These are **not** the account-linking credentials. Restart HA and unlink/relink
the skill if it was already linked so HA receives the AcceptGrant. Relinking can
affect Alexa routines; check their device references afterward.

## 5. Verify end to end

Enable development testing in the Alexa console where applicable. In the Alexa
app, enable each development skill and sign in to HA separately for each.

1. Discover devices; confirm only allowlisted supported entities appear.
2. Control a light by voice and query its state.
3. If proactive events are enabled, change that light in HA and verify Alexa's
   state updates without another voice command.
4. For Custom, ask "Alexa, ask home assistant what is the home status".
5. Verify linking persists beyond HA's access-token lifetime (about 30 minutes),
   exercising Alexa's refresh-token flow.

Logs are under `/aws/lambda/<name>-smart_home` and `<name>-custom`. The bridge
reports HTTP 401 as an account-linking problem and other upstream failures as
availability problems; inspect HA/proxy logs for detail. A missing link produces
a LinkAccount card for Custom skills. Never paste live tokens into tracked test
events or enable upstream body logging while troubleshooting.

## Local validation

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m unittest discover -s tests -v
terraform init -backend=false
terraform fmt -check -recursive
terraform validate
terraform test
docker build -t ha-alexa-connector:test addon
```

Terraform tests use a mocked AWS provider; they do not require AWS credentials
or create infrastructure. The archive provider builds the actual deployment ZIP.
Tests are not a substitute for the live Alexa/HA checks above.

## Upstream approach and licensing

This is an original implementation following the two HA documents above.
Research included [tjbaker/ha-alexa](https://github.com/tjbaker/ha-alexa)
(Apache-2.0), [haaska](https://github.com/mike-grant/haaska) (MIT), and
[Low-Frequency/hass-alexa-skill](https://github.com/Low-Frequency/hass-alexa-skill)
(no license found during investigation). No code is imported from those
projects. Terraform replaces SAM. Direct mode uses native HA OAuth; relay mode
uses a separate locally approved opaque-token OAuth service and an outbound
connector. This repository's code is MIT licensed.

For the opposite direction, HA controlling Echo devices, use HA's built-in
[Alexa Devices](https://www.home-assistant.io/integrations/alexa_devices/)
integration separately. It is not deployed by this stack.
