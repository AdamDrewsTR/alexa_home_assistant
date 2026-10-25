"""Run separately with HA 2026.10.3 and Python 3.14; uses real adapters and handlers."""

import asyncio
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from pathlib import Path
import shutil
import tempfile
from types import MappingProxyType
import unittest
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
from homeassistant import loader
from homeassistant.core import HomeAssistant, Context
from homeassistant.config_entries import ConfigEntry
from homeassistant.components.vacuum import VacuumEntityFeature
from homeassistant.components.water_heater import WaterHeaterEntityFeature
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.setup import async_setup_component

from addon.device_component import CONFIG_SCHEMA, DeviceConfig, DeviceView


class NativeDeviceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.temp.name)
        dr.async_setup(self.hass)
        await dr.async_load(self.hass)
        await er.async_load(self.hass)
        self.entities = er.async_get(self.hass)
        self.devices = dr.async_get(self.hass)
        entry = ConfigEntry(domain="example", title="Example", version=1, minor_version=1,
                            data={}, options={}, source="user", unique_id=None,
                            discovery_keys=MappingProxyType({}), subentries_data=[])
        self.hass.config_entries = Mock()
        self.hass.config_entries.async_get_entry.return_value = entry
        self.config_entry_id = entry.entry_id
        self.device = self.devices.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={("example", "physical-device")},
            name="Living room device", manufacturer="Example", model="Multi device")
        self.entries = {}
        self.config = None

    async def asyncTearDown(self):
        if self.config and self.config.report_task and not self.config.report_task.done():
            self.config.report_task.cancel()
            await asyncio.gather(self.config.report_task, return_exceptions=True)
        await self.hass.async_stop(force=True)
        self.temp.cleanup()

    def add_entity(self, entity_id, state, attributes=None):
        domain, name = entity_id.split(".")
        entry = self.entities.async_get_or_create(
            domain, "example", name, suggested_object_id=name, device_id=self.device.id)
        self.entries[entity_id] = entry
        self.hass.states.async_set(entry.entity_id, state, attributes or {})
        return entry

    async def configure(self):
        raw = {"locale": "en-US", "filter": {"include_entities": list(self.entries)},
               "entity_ids": {key: entry.id for key, entry in self.entries.items()},
               "client_id": "test-client", "client_secret": "test-secret",
               "endpoint": "https://api.amazonalexa.com/v3/events"}
        CONFIG_SCHEMA({"alexa_devices": raw})
        self.config = DeviceConfig(self.hass, raw)
        await self.config.async_initialize()
        self.view = DeviceView(self.config)

    async def request(self, namespace, name, endpoint_id=None, instance=None, payload=None):
        header = {"namespace": namespace, "name": name, "messageId": "request",
                  "payloadVersion": "3", "correlationToken": "test-correlation"}
        if instance:
            header["instance"] = instance
        message = {"directive": {"header": header, "payload": payload or {}}}
        if endpoint_id:
            message["directive"]["endpoint"] = {
                "endpointId": endpoint_id, "scope": {"type": "BearerToken", "token": "test-scope"}}
        return await self.view.handle(message, Context(user_id="test-user"))

    async def test_real_discovery_combines_plug_controls_and_temperature(self):
        self.add_entity("switch.plug", "on", {"friendly_name": "Plug"})
        self.add_entity("switch.child_lock", "off", {"friendly_name": "Child lock"})
        self.add_entity("sensor.temperature", "22.3", {"device_class": "temperature", "unit_of_measurement": "°C"})
        self.add_entity("sensor.energy", "0.5", {"device_class": "energy", "unit_of_measurement": "kWh"})
        await self.configure()
        result = await self.request("Alexa.Discovery", "Discover")
        endpoints = result["event"]["payload"]["endpoints"]
        self.assertEqual(len(endpoints), 1)
        endpoint = endpoints[0]
        self.assertEqual(endpoint["friendlyName"], "Living room device")
        interfaces = {cap["interface"] for cap in endpoint["capabilities"]}
        self.assertTrue({"Alexa.PowerController", "Alexa.ToggleController", "Alexa.TemperatureSensor"} <= interfaces)
        self.assertTrue(any("sensor.energy" in message for message in self.config.bundles()[1]))
        response = await self.request("Alexa", "ReportState", endpoint["endpointId"])
        self.assertEqual(response["event"]["endpoint"]["endpointId"], endpoint["endpointId"])
        self.assertEqual(response["event"]["header"]["correlationToken"], "test-correlation")
        self.assertIn("Alexa.TemperatureSensor", {prop["namespace"] for prop in response["context"]["properties"]})

    async def test_real_toggle_routes_only_to_its_member_and_rejects_unadvertised_instances(self):
        self.add_entity("switch.plug", "on")
        self.add_entity("switch.child_lock", "off", {"friendly_name": "Child lock"})
        await self.configure()
        bundle = next(iter(self.config.bundles()[0].values()))
        cap = next(cap for cap in bundle.capabilities if cap["interface"] == "Alexa.ToggleController")
        target = bundle.route("Alexa.ToggleController", cap["instance"])[0]["entity_id"]
        calls = []
        self.hass.services.async_register("switch", "turn_on", lambda call: calls.append(call))
        response = await self.request("Alexa.ToggleController", "TurnOn", bundle.endpoint_id, cap["instance"])
        await self.hass.async_block_till_done()
        self.assertEqual(response["event"]["header"]["name"], "Response")
        self.assertEqual(calls[0].data["entity_id"], target)
        response = await self.request("Alexa.ToggleController", "TurnOn", bundle.endpoint_id, "unknown")
        self.assertEqual(response["event"]["payload"]["type"], "INVALID_DIRECTIVE")
        self.assertEqual(len(calls), 1)
        response = await self.request("Alexa.PowerController", "TurnOn", "old-entity-id")
        self.assertEqual(response["event"]["payload"]["type"], "NO_SUCH_ENDPOINT")

    async def test_device_identity_and_member_selection_survive_entity_rename(self):
        entry = self.add_entity("light.before", "on", {"supported_color_modes": ["onoff"], "color_mode": "onoff"})
        await self.configure()
        before = next(iter(self.config.bundles()[0]))
        self.entities.async_update_entity(entry.entity_id, new_entity_id="light.after")
        self.hass.states.async_remove("light.before")
        self.hass.states.async_set("light.after", "on", {"supported_color_modes": ["onoff"], "color_mode": "onoff"})
        after = self.config.bundles()[0]
        self.assertEqual(list(after), [before])
        self.assertEqual(after[before].members[0]["entity_id"], "light.after")
        restarted = DeviceConfig(self.hass, self.config.raw)
        await restarted.async_initialize()
        self.assertEqual(list(restarted.bundles()[0]), [before])

    async def test_reusing_deleted_entity_name_does_not_expose_its_replacement(self):
        self.add_entity("switch.plug", "on")
        await self.configure()
        self.entities.async_remove("switch.plug")
        replacement = self.entities.async_get_or_create(
            "switch", "example", "replacement", suggested_object_id="plug", device_id=self.device.id)
        self.assertNotEqual(replacement.id, self.config.identities["switch.plug"])
        self.hass.states.async_set(replacement.entity_id, "off")
        bundles, warnings = self.config.bundles()
        self.assertEqual(bundles, {})
        self.assertTrue(any("replacement explicitly" in warning for warning in warnings))

    async def test_lock_alarm_and_garage_keep_native_capabilities_and_security(self):
        self.add_entity("lock.front", "locked", {"supported_features": 1})
        self.add_entity("alarm_control_panel.home", "disarmed", {
            "supported_features": 63, "code_format": "number", "code_arm_required": False})
        self.add_entity("cover.garage", "closed", {
            "device_class": "garage", "supported_features": 3, "current_position": 0})
        await self.configure()
        bundle = next(iter(self.config.bundles()[0].values()))
        interfaces = {cap["interface"] for cap in bundle.capabilities}
        self.assertTrue({"Alexa.LockController", "Alexa.SecurityPanelController", "Alexa.ModeController"} <= interfaces)
        calls = []
        self.hass.services.async_register("lock", "lock", lambda call: calls.append(call))
        response = await self.request("Alexa.LockController", "Lock", bundle.endpoint_id)
        await self.hass.async_block_till_done()
        self.assertEqual(response["event"]["header"]["name"], "Response")
        self.assertEqual(calls[0].data["entity_id"], "lock.front")
        state = self.hass.states.get("alarm_control_panel.home")
        self.hass.states.async_set(state.entity_id, "armed_away", dict(state.attributes))
        disarmed = []

        async def disarm(call):
            if call.data.get("code") != "1234":
                raise ServiceValidationError("A valid alarm PIN is required")
            disarmed.append(call.data["entity_id"])

        self.hass.services.async_register("alarm_control_panel", "alarm_disarm", disarm)
        response = await self.request("Alexa.SecurityPanelController", "Disarm", bundle.endpoint_id)
        self.assertEqual(response["event"]["header"]["name"], "ErrorResponse")
        self.assertEqual(disarmed, [])
        response = await self.request("Alexa.SecurityPanelController", "Disarm", bundle.endpoint_id,
                                      payload={"authorization": {"type": "FOUR_DIGIT_PIN", "value": "1234"}})
        self.assertEqual(response["event"]["header"]["name"], "Response")
        self.assertEqual(disarmed, ["alarm_control_panel.home"])

    async def test_proactive_report_uses_same_bundled_identity_and_instances(self):
        self.add_entity("switch.one", "on")
        self.add_entity("switch.two", "off")
        self.add_entity("sensor.temperature", "21", {"device_class": "temperature", "unit_of_measurement": "°C"})
        await self.configure()
        await self.config.set_authorized(True)
        self.config.publish = AsyncMock()
        bundle = next(iter(self.config.bundles()[0].values()))
        self.config.pending.add("switch.two")
        with patch("addon.device_component.async_send_changereport_message", new_callable=AsyncMock) as send:
            await self.config.report_changes()
        self.assertEqual(send.await_count, 1)
        sent_bundle, properties = send.call_args.args[2:]
        self.assertEqual(sent_bundle.endpoint_id, bundle.endpoint_id)
        toggle = next(cap for cap in bundle.capabilities if cap["interface"] == "Alexa.ToggleController")
        self.assertTrue(any(prop.get("instance") == toggle["instance"] for prop in properties))
        self.assertTrue(any(prop["namespace"] == "Alexa.TemperatureSensor" for prop in properties))

    async def test_accept_grant_requires_successful_native_auth(self):
        self.add_entity("switch.plug", "on")
        await self.configure()
        self.config.auth.async_do_auth = AsyncMock(return_value=None)
        response = await self.request("Alexa.Authorization", "AcceptGrant", payload={"grant": {"code": "test-code"}})
        self.assertEqual(response["event"]["header"]["name"], "ErrorResponse")
        self.assertFalse(self.config.authorized)
        self.config.auth.async_do_auth.return_value = "test-amazon-token"
        response = await self.request("Alexa.Authorization", "AcceptGrant", payload={"grant": {"code": "test-code"}})
        self.assertEqual(response["event"]["header"]["name"], "AcceptGrant.Response")
        self.assertTrue(self.config.authorized)

    async def test_native_device_type_matrix_discovers_distinct_physical_endpoints(self):
        samples = {
            "light": ("on", {"supported_color_modes": ["brightness"], "color_mode": "brightness", "brightness": 128}),
            "switch": ("on", {}),
            "sensor": ("21", {"device_class": "temperature", "unit_of_measurement": "°C"}),
            "binary_sensor": ("off", {"device_class": "motion"}),
            "lock": ("locked", {}),
            "alarm_control_panel": ("disarmed", {"code_arm_required": False, "supported_features": 63}),
            "cover": ("closed", {"device_class": "garage", "supported_features": 3}),
            "fan": ("on", {"supported_features": 1, "percentage": 50}),
            "climate": ("heat", {"supported_features": 1, "hvac_modes": ["heat", "off"], "temperature": 21,
                                  "current_temperature": 20, "min_temp": 7, "max_temp": 30}),
            "vacuum": ("docked", {"supported_features": VacuumEntityFeature.START | VacuumEntityFeature.RETURN_HOME}),
            "media_player": ("off", {"supported_features": 16384, "device_class": "tv"}),
            "humidifier": ("on", {"humidity": 45, "min_humidity": 10, "max_humidity": 90}),
            "water_heater": ("on", {"supported_features": WaterHeaterEntityFeature.ON_OFF}),
            "valve": ("closed", {"supported_features": 3, "current_position": 0}),
            "number": ("30", {"min": 0, "max": 100, "step": 1}),
            "input_boolean": ("on", {}),
            "button": ("unknown", {}),
            "remote": ("off", {}),
            "timer": ("idle", {}),
        }
        for domain, (state, attributes) in samples.items():
            self.device = self.devices.async_get_or_create(
                config_entry_id=self.config_entry_id,
                identifiers={("example", domain)}, name=f"Example {domain}")
            self.add_entity(f"{domain}.example", state, attributes)
        await self.configure()
        endpoints = (await self.request("Alexa.Discovery", "Discover"))["event"]["payload"]["endpoints"]
        self.assertEqual(len(endpoints), len(samples), self.config.bundles()[1])
        self.assertEqual(len({endpoint["endpointId"] for endpoint in endpoints}), len(samples))

    async def test_installed_component_loads_validates_and_registers_authenticated_views(self):
        target = Path(self.temp.name) / "custom_components/alexa_devices"
        shutil.copytree(Path(__file__).resolve().parents[2] / "addon/device_component", target)
        loader.async_setup(self.hass)
        self.hass.http = Mock()
        self.hass.config_entries.async_entries.return_value = []
        raw = {"alexa": {}, "alexa_devices": {"filter": {"include_entities": ["light.example"]}}}
        with patch("homeassistant.setup.async_process_deps_reqs", new_callable=AsyncMock):
            self.assertTrue(await async_setup_component(self.hass, "alexa_devices", raw))
        views = [call.args[0] for call in self.hass.http.register_view.call_args_list]
        self.assertEqual({view.url for view in views}, {"/api/alexa/smart_home", "/api/alexa_devices"})
        self.assertTrue(all(view.requires_auth for view in views))
        self.assertIn("alexa_devices", self.hass.config.components)

    async def test_real_metadata_and_state_event_http_delivery_preserve_bundled_identity(self):
        self.add_entity("switch.plug", "on")
        self.add_entity("sensor.temperature", "21", {"device_class": "temperature", "unit_of_measurement": "°C"})
        await self.configure()
        await self.config.set_authorized(True)
        self.config.auth.async_get_access_token = AsyncMock(return_value="test-amazon-token")
        received = []

        async def gateway(request):
            self.assertEqual(request.headers["Authorization"], "Bearer test-amazon-token")
            received.append(await request.json())
            return web.Response(status=202)

        app = web.Application()
        app.router.add_post("/events", gateway)
        server = TestServer(app)
        await server.start_server()
        self.addAsyncCleanup(server.close)
        self.config.smart["endpoint"] = str(server.make_url("/events"))
        async with AsyncExitStack() as stack:
            session = await stack.enter_async_context(aiohttp.ClientSession())
            stack.enter_context(patch("addon.device_component.async_get_clientsession", return_value=session))
            stack.enter_context(patch(
                "homeassistant.components.alexa.state_report.async_get_clientsession", return_value=session))
            bundles = self.config.bundles()[0]
            endpoint_id = next(iter(bundles))
            await self.config.publish(bundles)
            await self.config.publish(bundles)
            self.assertEqual(len(received), 1)
            self.assertEqual(received[0]["event"]["payload"]["endpoints"][0]["endpointId"], endpoint_id)
            self.config.pending.add("sensor.temperature")
            await self.config.report_changes()
            report = received[-1]["event"]
            self.assertEqual(report["header"]["name"], "ChangeReport")
            self.assertEqual(report["endpoint"]["endpointId"], endpoint_id)
            self.assertIn("Alexa.TemperatureSensor",
                          {prop["namespace"] for prop in report["payload"]["change"]["properties"]})
            self.config.selected.clear()
            await self.config.publish(self.config.bundles()[0])
            report = received[-1]["event"]
            self.assertEqual(report["header"]["name"], "DeleteReport")
            self.assertEqual(report["payload"]["endpoints"], [{"endpointId": endpoint_id}])

    async def test_doorbell_event_keeps_identity_and_slow_cloud_events_do_not_lock_commands(self):
        self.add_entity("event.doorbell", datetime.now(timezone.utc).isoformat(), {"device_class": "doorbell"})
        self.add_entity("switch.chime", "on")
        await self.configure()
        await self.config.set_authorized(True)
        started, finish = asyncio.Event(), asyncio.Event()

        async def publish(bundles):
            self.assertFalse(self.config.lock.locked())
            started.set()
            await finish.wait()

        self.config.publish = publish
        self.config.doorbells.append("event.doorbell")
        bundle = next(iter(self.config.bundles()[0].values()))
        with patch("addon.device_component.async_send_doorbell_event_message", new_callable=AsyncMock) as send:
            task = asyncio.create_task(self.config.report_changes())
            self.config.report_task = task
            try:
                await asyncio.wait_for(started.wait(), 2)
                async with asyncio.timeout(0.2):
                    async with self.config.lock:
                        response = await self.request("Alexa", "ReportState", bundle.endpoint_id)
                self.assertEqual(response["event"]["header"]["name"], "StateReport")
                finish.set()
                await task
            finally:
                finish.set()
                await asyncio.gather(task, return_exceptions=True)
            self.assertEqual(send.await_count, 1)
            self.assertEqual(send.call_args.args[2].endpoint_id, bundle.endpoint_id)

    async def test_cover_range_and_native_playback_instance_route_to_original_services(self):
        self.add_entity("cover.blind", "open", {"supported_features": 15, "current_position": 50})
        self.add_entity("fan.ceiling", "on", {"supported_features": 1, "percentage": 30})
        await self.configure()
        bundle = next(iter(self.config.bundles()[0].values()))
        calls = []
        self.hass.services.async_register("cover", "set_cover_position", lambda call: calls.append(call))
        self.hass.services.async_register("cover", "stop_cover", lambda call: calls.append(call))
        range_cap = next(cap for cap in bundle.capabilities
                         if cap["interface"] == "Alexa.RangeController"
                         and bundle.route(cap["interface"], cap["instance"])[0]["entity_id"] == "cover.blind")
        response = await self.request("Alexa.RangeController", "SetRangeValue", bundle.endpoint_id,
                                      range_cap["instance"], {"rangeValue": 25})
        self.assertEqual(response["event"]["header"]["name"], "Response")
        self.assertTrue(any(prop.get("instance") == range_cap["instance"] and prop["value"] == 25
                            for prop in response["context"]["properties"]))
        playback = next(cap for cap in bundle.capabilities if cap["interface"] == "Alexa.PlaybackController")
        self.assertEqual(playback["instance"], "cover.stop")
        response = await self.request("Alexa.PlaybackController", "Stop", bundle.endpoint_id, playback["instance"])
        self.assertEqual(response["event"]["header"]["name"], "Response")
        await self.hass.async_block_till_done()
        self.assertEqual([call.service for call in calls], ["set_cover_position", "stop_cover"])
        self.assertTrue(all(call.data["entity_id"] == "cover.blind" for call in calls))

    async def test_metadata_refreshes_expired_token_and_does_not_save_failed_updates(self):
        self.add_entity("switch.plug", "on")
        await self.configure()
        await self.config.set_authorized(True)
        self.config.auth.async_get_access_token = AsyncMock(side_effect=["expired", "fresh", "fresh"])
        self.config.auth.async_invalidate_access_token = Mock()
        received = []
        fail = False

        async def gateway(request):
            received.append(await request.json())
            if len(received) == 1:
                return web.json_response({"payload": {"code": "INVALID_ACCESS_TOKEN_EXCEPTION"}}, status=401)
            return web.Response(status=500 if fail else 202)

        app = web.Application()
        app.router.add_post("/events", gateway)
        server = TestServer(app)
        await server.start_server()
        self.addAsyncCleanup(server.close)
        self.config.smart["endpoint"] = str(server.make_url("/events"))
        async with AsyncExitStack() as stack:
            session = await stack.enter_async_context(aiohttp.ClientSession())
            stack.enter_context(patch("addon.device_component.async_get_clientsession", return_value=session))
            await self.config.publish(self.config.bundles()[0])
            self.config.auth.async_invalidate_access_token.assert_called_once()
            self.assertEqual([item["event"]["payload"]["scope"]["token"] for item in received], ["expired", "fresh"])
            before = dict(self.config.settings["published"])
            self.devices.async_update_device(self.device.id, name_by_user="Renamed device")
            fail = True
            with self.assertLogs("addon.device_component", level="ERROR") as logs:
                await self.config.publish(self.config.bundles()[0])
            self.assertIn("HTTP 500", str(logs.output))
            self.assertEqual(self.config.settings["published"], before)
