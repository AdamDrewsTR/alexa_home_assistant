# Alexa Outbound Connector

## Companion phone location (experimental)

Version **0.3.0** adds an optional location relay for up to **20 phones** using
the official Home Assistant Companion app. Each phone is enrolled and revoked
independently and keeps its existing tracker/person association. No VPN,
router port forwarding, extra tracking app, or remote HA frontend is required.

Deploy the updated Terraform relay, update this app, then create a new
configuration preview, back up/apply, validate, and restart HA to install the
updated local integration. Register each Companion app on home Wi-Fi first.
Open **Phone location** in this app's Web UI, enroll the intended phone, and
refresh until **Cloud enrolled** appears. The phone list requires HA
administrator access; no credentials need to be copied manually.

In the Companion app's server connection settings, retain your **Internal URL**
and **home Wi-Fi SSID**, and set **External URL** to the HTTPS relay URL shown.
Grant Always/background location permission and enable location sensors.
Associate each tracker with its person in HA and use normal zone/person
automations. An existing HA Cloud webhook can take priority over this URL.
Remote dashboards, service calls, and remote login/token refresh are not
supported; the internal URL remains necessary at home.

The relay permits location, configuration/zones, registration updates, and phone
sensor synchronization only. iOS encrypted payloads remain encrypted through AWS;
the checked Android release uses HTTPS without payload encryption. Do not share
webhook URLs or enable AWS request-body logging. Enrollment is stored locally
with owner-only permissions; only ID hashes are synced over the connector.
Cloud enrollment expires within two minutes without polling. Revocation is
enforced locally immediately.

There is no offline queue or location history in AWS. A missed update leaves
HA's last-known location, not an automatic away state. App retries may deliver
delayed measurements. The status shows when a location request was last accepted
by HA this connector session, not a guarantee of current GPS freshness. Test
locked-phone cellular updates, token expiry, connector restart, and physical
zone transitions before relying on automations. Use additional presence signals
and never GPS alone to unlock a door.

## Alexa setup

Provision AWS and Alexa with `bootstrap.py` from the repository first. Its
`install-ha` step configures this app automatically after local HA consent.

Open **Web UI** to select entities, preview/back up/apply configuration, check
it, and approve a restart. Existing unrelated Alexa/intent YAML is not replaced.
Submitting shows a spinner and progress message and disables further submissions
until the result loads. Configuration checks can take up to two minutes.
Failures show a retry action; returning with the browser's Back button restores
the controls.
The dashboard reads HA's existing floor, area, device, and entity registries.
Use room navigation, search, and entity-type filters to narrow the list.
Device checkboxes select the visible controls and sensors on that device;
**Select visible** and **Clear visible** respect your current filters. Existing
managed selections are preselected, including unavailable entities, and
filtering does not silently remove selections. No entities are preselected on
first setup. Account-linking requests update without reloading your selections.

**One physical device becomes one native Alexa endpoint.** Setup installs the
`alexa_device_endpoints` custom integration inside HA, which composes HA's native Alexa
capabilities and handlers without patching HA. Selected controls and supported
sensors belonging to the same HA device share discovery, control, state queries,
and proactive state reporting. Endpoint IDs survive restarts and renames;
HA registry IDs also keep selected entities selected after renaming.

Coverage follows HA's Alexa adapter registry, not a fixed device shortlist:
lights, plugs/switches, supported sensors, locks, security panels, garage doors,
covers, fans, thermostats, media players, vacuums, humidifiers, water heaters,
valves, numeric controls, scenes, scripts, cameras, and other native adapters.
Supplemental Alexa-native mappings add humidity sensors, weather temperature
and humidity, battery-level sensors, and basic siren/lawn-mower power control.
Alexa's Smart Home API has no equivalent interfaces for every Matter sensor
type; illuminance, air quality, smoke/CO, pressure, flow, and energy readings
are not supported by this integration. Locks and alarm commands use HA handlers
and service-level authorization/PIN validation. Alexa's locale and capability
restrictions still apply.
Matter Hub can expose additional Matter device types through Alexa's native
Matter support; that path is distinct from the public interfaces available to a
third-party Smart Home skill. Matter Hub is a better fit for those sensor types
or simpler setup, while this integration is an alternative rather than a
feature-complete replacement.

Additional on/off controls use named `Alexa.ToggleController` instances.
Range, mode, and toggle components receive stable instance IDs. When Alexa
allows only one specific capability (for example, one temperature sensor),
the conflicting capability is omitted and listed under **Compatibility
details**, rather than exported as an unexpected extra device. Unsupported
measurements such as many energy sensors are also reported explicitly.
Scenes, scripts, cameras, and entities without a physical HA device remain
independent endpoints because they represent separate actions or lifecycles.
The dashboard does not create Alexa room groups.
Physical device cards use the device's area (or one stable entity-area fallback)
so entity area overrides do not split a single endpoint across rooms.
Camera streaming still requires HA's native streaming/HTTPS prerequisites;
the outbound command relay does not carry video.

After upgrading from a per-entity setup, preview/apply and restart HA, remove
the old individual devices in Alexa, and relink the Smart Home skill once.
Update both the AWS relay (review/apply the Terraform plan) and connector:
version 0.2.0 uses bounded response fragments for larger discovery results.
Mixed relay/connector versions cannot complete commands. Publish the committed
code to the app repository's default branch, then in **Settings > Apps > App
store** check for updates and update this app to **0.3.0**. The wizard's
`install-ha` command does not upgrade an already installed app.
See the [repository upgrade instructions](../README.md#upgrading-an-existing-ha-os-installation)
for the exact Terraform commands. Check Alexa routines referencing old devices
after rediscovery.
The 0.2.1 patch added submission feedback without changing the relay protocol.
Version 0.2.2 renames our YAML integration to `alexa_device_endpoints`, avoiding
HA's built-in `alexa_devices` integration. For a "does not support YAML" error,
update the app, create a new preview, apply, validate, and restart HA. The old
tool-owned include is migrated and the old owned component directory is moved
outside `custom_components` to a timestamped backup. Unowned configuration is
never replaced. This fix needs no Terraform apply. Relink if the old endpoint
runtime had already been linked, because the integration's identity/auth store
now uses the new domain.
The new integration has its own local event-authorization storage. Subsequent
device metadata updates/removals use native AddOrUpdateReport/DeleteReport.
There is no parallel legacy per-entity runtime. Integration tests target HA
2026.10.3; verify compatibility before upgrading HA because native adapter APIs
are internal HA APIs.

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
