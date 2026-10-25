# Alexa Outbound Connector

Provision AWS and Alexa with `bootstrap.py` from the repository first. Its
`install-ha` step configures this app automatically after local HA consent.

Open **Web UI** to select entities, preview/back up/apply configuration, check
it, and approve a restart. Existing unrelated Alexa/intent YAML is not replaced.
The dashboard reads HA's existing floor, area, device, and entity registries.
Use room navigation, search, and entity-type filters to narrow the list.
Device checkboxes select the visible controls and sensors on that device;
**Select visible** and **Clear visible** respect your current filters. Existing
managed selections are preselected, including unavailable entities, and
filtering does not silently remove selections. No entities are preselected on
first setup. Account-linking requests update without reloading your selections.

Device cards group related entities for selection **in this UI**. They do not
merge separate HA entities into one Alexa device or create Alexa room groups.
HA determines which selected entities and capabilities Alexa can discover;
some sensor types are not supported. True multi-entity Alexa bundles would
also require coordinated discovery, command routing, and proactive reporting.

If the configuration check fails, do not restart: inspect HA logs and restore
the timestamped `.alexa-backup` files as appropriate.
The check's diagnostic is shown on the result page and in this app's **Log**
tab, not necessarily HA Core's normal logs. Retry the check without applying
the files again. Saved previews are private app data; a new preview replaces
an older one, and concurrent edits require a fresh preview.

In Alexa, start account linking. Compare the displayed code with the pending
request here and approve only if you initiated it. Refresh the page if needed.
Revoke linked accounts here when removing access.
Complete configuration validation and restart HA before linking: if HA hasn't
loaded its Alexa endpoints, the OAuth approval can succeed but Alexa's
subsequent authorization/discovery commands fail. The app log reports the
local HTTP status and endpoint without logging command bodies or tokens.

The cloud connection is outbound only. No router ports, domains, or public HA
URL are required. Never publish the approval UI. This app has administrative HA
API access and configuration-file access; keep its options and backups private.

This is an experimental self-managed integration. AWS usage is metered and live
Alexa/HA acceptance testing is required.
