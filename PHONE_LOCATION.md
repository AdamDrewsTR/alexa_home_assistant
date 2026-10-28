# Companion phone location setup and troubleshooting

This experimental relay lets an already registered Home Assistant Companion app
send location and sensor updates without a VPN or router port forwarding.
It does **not** provide remote dashboards, login, or a general HA API tunnel.
Keep local HA access working before testing cellular delivery.

## What goes where

| Setting | Where to configure it | Value |
|---|---|---|
| Connector WebSocket URL | HA > Settings > Apps (or Add-ons) > Alexa Outbound Connector > Configuration | Terraform's `relay.websocket_url`: `wss://<socket-api-id>.execute-api.<region>.amazonaws.com/connector` |
| Connector key | Same add-on Configuration page | Existing private connector key for that deployment; do not generate a new one just to change a URL |
| Phone external address | Android Companion app > Settings > Companion app > select the server > Home Assistant URL | HTTPS origin shown in the connector's Phone location page: `https://<http-api-id>.execute-api.<region>.amazonaws.com` |
| Home Wi-Fi | Same phone server settings > Home network | Your home Wi-Fi SSID; select the networks on which local HA is reachable |
| Phone internal address | Same phone server settings > Home network > Internal connection URL | Working local HA URL, including its port if needed |
| Background GPS permission | Android Settings > Apps > Home Assistant > Permissions > Location | Allow all the time, with Precise location enabled |
| Location collection | Companion app > Settings > Companion app > Manage sensors | Enable Background location for this server and Zone-based tracking |
| Person association | HA > Settings > People > your person | Select the existing phone tracker |

Phone menu labels vary by version. The **Home Assistant URL** label is the
external address on the Android screen with **Home network** and **Persistent
connection**. Configure Home network and its internal address **before**
replacing that main address. If the menus differ, do not overwrite your working
local URL until you have identified the separate internal setting.

The two AWS API IDs are different: do not convert the WSS address into HTTPS
and assume it is the phone endpoint. Copy each from its corresponding output/UI.
**Do not set HA's global external/network URL to the restricted relay.**

## Prepare Home Assistant

1. Deploy the phone-capable AWS relay in the existing deployment's account and
   state, then update the connector add-on. Save its matching WSS URL and key.
   Verify cloud connectivity before configuring the phone.
2. If the installed local integration lacks the phone-registration endpoint,
   use the connector Web UI to preview the existing entity selection, back
   up/apply, and validate. Restart **HA Core** only after validation succeeds.
   Restarting just the add-on does not reload HA's custom integration.
   This phone-support update does not itself require Alexa relinking.
3. Confirm the phone is registered under **Settings > Devices & services >
   Mobile App**. If it already exists, keep that registration.
4. Open the connector's **Phone location** page with an HA administrator account.
   Enroll the intended phone and wait for **Cloud enrolled**.
5. After obtaining a real location fix, assign the tracker to its Person.
   Use normal Person/device-tracker home/away or zone automations. Configure
   geographic zones in HA's zone settings; a Wi-Fi SSID is not a geographic zone.

No separate location-tracking integration, public HA URL, router port forward,
or phone VPN is needed for this relay path. The enrolled phone's native
`mobile_app` tracker remains the source of location in HA.

## Set up the phone

1. Register the Companion app with HA on home Wi-Fi. Keep the existing phone
   registration and tracker; do not remove/re-add the server to troubleshoot.
2. In the connector Web UI, open **Phone location**, enroll the intended phone,
   and refresh until **Cloud enrolled** appears.
3. Configure separate local and external addresses in the Companion app.
   On Android versions showing **Home Assistant URL**, **Home network**, and
   **Persistent connection**:
   - Under **Home network**, select your home Wi-Fi SSID and set
     **Internal connection URL** to your working local HA address, for example
     `http://homeassistant.local:8123` or your local IP and port.
   - Set the main **Home Assistant URL** to the HTTPS origin displayed by the
     connector's Phone location page, without a trailing slash. Do not append
     `/connector` or a webhook path.
   - Leave **Persistent connection** unchanged for this setup.
   Labels vary by app version and platform. If no separate internal address is
   available, stop before replacing the working local address.
4. On Android, grant **Allow all the time** location access and enable
   **Precise location** in the phone's system app permissions. The Companion
   app also needs location access to identify home Wi-Fi and select its internal
   address. Under **Manage sensors**, enable **Background location** for the
   server and **Zone-based tracking**. The optional **Location zone** sensor is
   not required for normal tracker/person zone automations.
5. Confirm the dashboard still opens on home Wi-Fi. Associate the phone's
   existing `device_tracker` with its Person in HA.

These are **phone app settings**, not HA's global network URL. They do not
change DNS resolution for `homeassistant.local`. An existing HA Cloud webhook
can take priority over the external URL.

On cellular, failure to load the dashboard is expected: the external relay only
accepts the supported mobile webhooks. Background delivery can work without a
dashboard. At home the app must use its internal address for the dashboard,
registration, and authentication maintenance. If the dashboard also fails at
home, check the internal address, selected Wi-Fi network, and permissions; do
not assume the relay can serve the frontend.

## Verify delivery, not just enrollment

First check **Developer Tools > States** on a local computer for the phone's
`device_tracker` entity. An `unknown` state without coordinates means it does
not currently have a usable location. Confirm a GPS fix on home Wi-Fi first.
Do not use **Set state** to test delivery: that only changes HA's representation.

For a controlled cellular test:

1. Turn off the phone's Wi-Fi and leave mobile data enabled.
2. On a computer connected to local HA, open **Developer Tools > Actions** and
   select the intended phone's `notify.mobile_app_...` action. Send:

   ```yaml
   message: request_location_update
   ```

3. Refresh the connector's **Phone location** page manually; it does not update
   live. Look for **last location accepted ... seconds ago**.
4. Inspect the tracker's coordinates and state in HA to confirm application of
   the update. A successful webhook response alone is not proof that HA applied
   a fresh GPS measurement.

The notification command uses push delivery, not the phone's dashboard.
Push delivery and Android's location scheduling can delay the response.
**Cloud enrolled** only confirms enrollment synchronization. **No location
received this session** means this connector process has not successfully
forwarded an `update_location` request. The accepted counter resets when the
connector restarts and records successful local webhook responses, not the
age of the underlying GPS measurement.

Turning Wi-Fi off does not mean leaving the geographic home zone. A phone
physically at home should still report `home`. HA's **Last updated** timestamp
can remain unchanged for identical state and attributes; Activity is not a
receipt log for every GPS report.

Finally test actual departure and arrival with the phone locked, and repeat
after a connector restart and HA access-token expiry. A requested update is
not proof that automatic background zone transitions work reliably.

## Update frequency

The connector's **10-second polling interval is not a phone GPS interval**.
It synchronizes enrollment and account-linking status with AWS. Location
requests are forwarded when they arrive, not held until the next poll.

Android schedules updates based on movement, zone crossings, background
execution, and explicit requests. Several minutes between updates while
stationary can be normal. This is presence tracking, not a 10-second live
GPS feed.

## Troubleshooting

| Symptom | Check |
|---|---|
| Cloud connection unavailable | The connector's WSS URL must end in `/connector`, with no surrounding whitespace or trailing slash on older installed versions. This is separate from the phone's HTTPS URL. Check the original connector key and selected AWS account; do not rotate credentials blindly. |
| Empty Alexa linked-accounts panel | This list is fetched over the connector's WebSocket and cleared on disconnect. An empty panel alone does not mean cloud grants were deleted. Restore connectivity before relinking. |
| Waiting for updated cloud relay | The connector has not received a phone-relay URL. Check connection/polling and that the deployed Lambda includes phone support and its `HTTP_URL` setting. Updating the add-on alone does not update AWS. |
| Cloud enrolled, no accepted location | Verify phone tracking permissions, per-server background location, and external URL. Use the controlled test above. If it still fails, inspect the phone's location-tracking troubleshooting history and connector errors; do not repeatedly re-enroll. |
| Tracker remains `unknown` | Confirm location collection works locally first, then test relay delivery. Enrollment does not enable phone GPS tracking. |
| Dashboard fails only on cellular | Expected for this restricted relay; do not log out or delete the server registration. |

When deploying, use the saved Terraform state, expected AWS account, and
`.setup/relay.tfvars.json`. Changing an AWS profile does not select a different
Terraform state. Do not recreate gateways merely to troubleshoot connectivity.
Never share connector keys, OAuth/event secrets, or webhook IDs.

There is no cloud location history or offline delivery queue. Missed updates
leave the last-known location, not an automatic away state. Use conservative
automations and additional presence signals; never GPS alone to unlock a door.

See the official Companion documentation for
[network selection](https://companion.home-assistant.io/docs/troubleshooting/networking/#setting-up-the-app)
and [location behavior](https://companion.home-assistant.io/docs/core/location/).
