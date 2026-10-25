# Alexa Outbound Connector

Provision AWS and Alexa with `bootstrap.py` from the repository first. Its
`install-ha` step configures this app automatically after local HA consent.

Open **Web UI** to select entities, preview/back up/apply configuration, check
it, and approve a restart. Existing unrelated Alexa/intent YAML is not replaced.
If the configuration check fails, do not restart: inspect HA logs and restore
the timestamped `.alexa-backup` files as appropriate.

In Alexa, start account linking. Compare the displayed code with the pending
request here and approve only if you initiated it. Refresh the page if needed.
Revoke linked accounts here when removing access.

The cloud connection is outbound only. No router ports, domains, or public HA
URL are required. Never publish the approval UI. This app has administrative HA
API access and configuration-file access; keep its options and backups private.

This is an experimental self-managed integration. AWS usage is metered and live
Alexa/HA acceptance testing is required.
