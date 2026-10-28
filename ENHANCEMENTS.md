# Enhancement roadmap

Proposed improvements for a more robust self-managed alternative to selected
Home Assistant Cloud features. This is a roadmap, not a list of implemented
capabilities or a promise of feature parity.

## First priority: easy to set up, understand, and maintain

The highest-value work is reducing the knowledge and manual steps needed to
deploy and operate the existing features, not adding more integrations.
Reliability checks should be built into the user journey rather than requiring
users to interpret logs, Terraform state, or AWS metrics.

### Guided deployment and upgrades

Provide one resumable flow that checks prerequisites, shows the selected AWS
account/region and existing deployment, previews changes, and requests approval
before applying them. Clearly distinguish first installation, upgrade, and
recovery; never silently create a replacement deployment.

Explain costs, permissions, and the restricted relay's capabilities before
deployment. Label each step as automatic or manual, explain why it is needed,
and verify its outcome before reporting success.

Show cloud, add-on, and HA integration versions together. Explain exactly which
component needs updating or restarting and whether relinking is actually
required. Preserve existing options, selections, credentials, and identities.

### Guided phone setup and end-to-end checks

Guide users through the connector WSS URL, phone HTTPS URL, internal HA address,
home Wi-Fi selection, and background/precise location permissions.
Normalize harmless URL whitespace and validate stage paths before connecting.

Show separate checks for cloud connectivity, enrollment, local location fixes,
requested cellular delivery, and automatic locked-phone zone transitions.
Explain that dashboard access is unavailable through the restricted relay and
that ten-second connector polling is not a GPS update interval.

### Plain-language status and next actions

Replace ambiguous empty panels and generic waiting messages with states such
as disconnected, connecting, enrolled, awaiting first location, and last
successful delivery. Distinguish an unavailable linked-account list from a
confirmed empty list.

Explain what is working, what is not verified, and the next safe action.
Keep advanced diagnostics available without making AWS console access or log
interpretation the normal troubleshooting path. Never imply that enrollment,
an open socket, or an HTTP success proves the entire feature works.

### Usability acceptance criteria

A new user should be able to follow the documented flow without guessing which
URL belongs in which app, editing secrets by hand, or knowing Terraform
internals. An existing user should be able to upgrade without losing links or
changing stable URLs. Failed steps should preserve working configuration and
provide an actionable explanation rather than recommending a reset.

## Supporting reliability and recovery

### HA-visible relay health

Expose diagnostic entities in HA for connector connectivity, last successful
cloud exchange, request failures, and per-phone location-delivery age.
Use them in ordinary HA notifications and conservative presence automations.

Distinguish a connected socket from successful polling and command delivery.
Per-phone receipt time is not GPS measurement time; a fresh receipt can contain
a delayed measurement. A stale phone must not automatically mean away.

### Deployment safeguards

Verify the expected AWS account and region before deploying. Preserve state
and setup files, warn about unexpected gateway/table creation or replacement,
and detect duplicate deployments before suggesting recovery actions.

Verify actual deployed Lambda code hashes against intended artifacts, rather
than treating a no-change Terraform plan as proof of running-code identity.
Make cloud, add-on, and loaded HA integration versions independently visible.
Build on the existing account-ID and prevent-destroy guards; these do not
protect against every wrong-state or manual deletion scenario.

### Actionable phone diagnostics

Show last webhook receipt, message type, forwarding success/failure, and last
accepted location separately. Clearly label session-only versus persistent
counters and whether the page refreshes automatically.

Explain where failure occurred: phone collection, delivery to AWS, enrollment,
connector forwarding, or HA processing. Successful HTTP responses alone do not
prove that a fresh location was applied to the intended tracker.

Keep webhook IDs, credentials, coordinates, and request bodies out of
diagnostic logs. Any added diagnostics should have explicit retention bounds.

### Backup and recovery workflow

Provide a documented, verifiable backup/restore procedure for Terraform state,
private setup files, connector options/enrollments, and managed HA configuration.
Protect credentials with restricted access and appropriate encrypted storage.

Recovery should inventory the original deployment before creating replacements,
preserve original URLs and grants where possible, and distinguish restoring
connectivity from rotating credentials or relinking Alexa.
Never silently replace working options with stale or blank saved values.

## Larger optional features

### Google Assistant integration

Explore a separate Google Assistant integration using the outbound connector.
This needs its own Google platform setup, account linking, device mapping,
state reporting, compatibility tests, and operational documentation.
It is not enabled simply by adding another forwarding route.

### Full remote dashboard and Companion access

An outbound reverse tunnel could provide full HA UI access without inbound
router port forwarding. Treat it as a separate architecture and opt-in security
model, not an expansion of the phone message allowlist.

It must support frontend assets, authentication, REST, long-lived WebSockets,
larger responses, appropriate limits, and reliable streaming/backpressure.
The existing bounded Lambda/API Gateway command relay is not a general HTTP
proxy. Evaluate established tunnel solutions before building a new one.

No router ports does not mean no public exposure: this would make HA's frontend
and login internet-accessible through the tunnel.

## Boundaries and acceptance

- Companion push notifications use the existing mobile-app ecosystem; this relay
  does not implement a replacement push service.
- Voice/STT/TTS services need separate providers or local engines; forwarding
  alone does not reproduce HA Cloud's hosted services.
- Prefer reliability work before new integrations. Avoid adding configuration
  or observability that exposes secrets or silently expands remote access.
- Verify improvements with targeted tests and live acceptance checks appropriate
  to the feature. One requested cellular update is not proof of reliable
  automatic departure/arrival behavior.

Related guides: [Phone location](PHONE_LOCATION.md),
[Troubleshooting and recovery](TROUBLESHOOTING.md), and
[project setup](README.md).
