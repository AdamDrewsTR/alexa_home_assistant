import tempfile
import unittest
from html.parser import HTMLParser
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from addon.connector import Connector
from addon.ha_config import apply, generate, selected_entities
from addon.ui import dashboard, organize


def example_home():
    """Synthetic home shared by grouping and transport tests."""
    registry = {
        "floors": [{"floor_id": "ground", "name": "Ground floor", "level": 0},
                   {"floor_id": "upper", "name": "First floor", "level": 1}],
        "areas": [{"area_id": "living", "name": "Living room", "floor_id": "ground"},
                  {"area_id": "kitchen", "name": "Kitchen", "floor_id": "ground"},
                  {"area_id": "bedroom", "name": "Bedroom", "floor_id": "upper"}],
        "devices": [
            {"id": "lamp", "name": "Reading lamp", "area_id": "living",
             "manufacturer": "Example", "model": "Color light"},
            {"id": "air", "name": "Air monitor", "area_id": "living",
             "manufacturer": "Example", "model": "Room sensor"},
            {"id": "plug", "name": "Coffee station", "area_id": "kitchen",
             "manufacturer": "Example", "model": "Smart plug"},
            {"id": "thermostat", "name": "Bedroom comfort", "area_id": "bedroom",
             "manufacturer": "Example", "model": "Thermostat"},
        ],
        "entities": [
            {"entity_id": "light.reading", "device_id": "lamp"},
            {"entity_id": "sensor.lamp_power", "device_id": "lamp"},
            {"entity_id": "sensor.temperature", "device_id": "air"},
            {"entity_id": "sensor.humidity", "device_id": "air"},
            {"entity_id": "switch.coffee", "device_id": "plug"},
            {"entity_id": "sensor.coffee_energy", "device_id": "plug"},
            {"entity_id": "climate.bedroom", "device_id": "thermostat"},
            {"entity_id": "sensor.bedroom_temperature", "device_id": "thermostat"},
        ],
    }
    states = [
        {"entity_id": entity_id, "state": value,
         "attributes": {"friendly_name": name, "unit_of_measurement": unit}}
        for entity_id, name, value, unit in (
            ("light.reading", "Reading light", "on", ""),
            ("sensor.lamp_power", "Power consumption", "8.4", "W"),
            ("sensor.temperature", "Temperature", "22.1", "C"),
            ("sensor.humidity", "Humidity", "46", "%"),
            ("switch.coffee", "Coffee machine", "off", ""),
            ("sensor.coffee_energy", "Energy today", "0.12", "kWh"),
            ("climate.bedroom", "Thermostat", "heat", ""),
            ("sensor.bedroom_temperature", "Room temperature", "20.4", "C"),
            ("scene.evening", "Evening scene", "unknown", ""),
        )
    ]
    return states, registry


class Inputs(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.entities = {}
        self.feed(html)

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        if tag == "input" and attributes.get("name") == "entity":
            self.entities[attributes["value"]] = "checked" in attributes


class DashboardTests(unittest.TestCase):
    def test_physical_devices_stay_together_despite_entity_area_overrides(self):
        states, registry = example_home()
        registry["entities"][1]["area_id"] = "kitchen"
        groups = organize(states, registry)
        self.assertEqual([area["name"] for area in groups], ["Kitchen", "Living room", "Bedroom", "Unassigned"])
        living = next(area for area in groups if area["id"] == "living")
        monitor = next(device for device in living["devices"] if device["name"] == "Air monitor")
        self.assertEqual({entity["id"] for entity in monitor["entities"]},
                         {"sensor.temperature", "sensor.humidity"})
        kitchen = next(area for area in groups if area["id"] == "kitchen")
        self.assertFalse(any(device["name"] == "Reading lamp" for device in kitchen["devices"]))
        lamp = next(device for device in living["devices"] if device["name"] == "Reading lamp")
        self.assertEqual({entity["id"] for entity in lamp["entities"]}, {"light.reading", "sensor.lamp_power"})

    def test_existing_selection_is_preserved_even_if_absent_disabled_or_unsupported(self):
        states, registry = example_home()
        registry["entities"][1]["disabled_by"] = "user"
        states.append({"entity_id": "update.router", "state": "off"})
        selected = ["sensor.lamp_power", "sensor.missing", "input_boolean.existing"]
        page = dashboard(states, registry, selected, "csrf", True, [], [], "")
        inputs = Inputs(page).entities
        self.assertTrue(all(inputs[entity_id] for entity_id in selected))
        self.assertNotIn("update.router", inputs)
        self.assertFalse(inputs["light.reading"])
        self.assertIn("unavailable", page)

    def test_dashboard_escapes_registry_names_states_and_linking_data(self):
        states, registry = example_home()
        registry["devices"][0]["name"] = "<script>alert('device')</script>"
        registry["areas"][0]["name"] = "'><img src=x>"
        states[0]["state"] = "<script>alert('state')</script>"
        page = dashboard(states, registry, [], "csrf", True,
                         [{"code": "<unsafe>", "id": "'bad", "client": "<client>"}], [], "")
        self.assertNotIn("<script>alert", page)
        self.assertNotIn("<img src=x>", page)
        self.assertIn("&lt;unsafe&gt;", page)
        self.assertEqual(len(Inputs(page).entities), len(states))
        self.assertIn("one native endpoint", page)

    def test_native_endpoint_details_show_real_bundle_names_and_compatibility_limits(self):
        states, registry = example_home()
        status = {"devices": [{
            "name": "Coffee <station>", "entities": ["switch.coffee"],
            "capabilities": ["Alexa", "Alexa.PowerController", "Alexa.EndpointHealth"],
        }], "warnings": ["sensor.coffee_energy: no <supported> Alexa capability"]}
        page = dashboard(states, registry, ["switch.coffee"], "csrf", True, [], [], "",
                         endpoint_status=status)
        self.assertIn("Native device endpoints active", page)
        self.assertIn("Alexa: Coffee &lt;station&gt; / 3 capabilities", page)
        self.assertIn("Compatibility details", page)
        self.assertIn("no &lt;supported&gt; Alexa capability", page)

    def test_selection_is_read_from_owned_yaml_and_rejects_bad_filters(self):
        with tempfile.TemporaryDirectory() as directory:
            from pathlib import Path

            root = Path(directory)
            (root / "configuration.yaml").write_text("default_config:\n")
            self.assertEqual(selected_entities(root), [])
            apply(root, generate(root, ["light.reading", "sensor.temperature"]))
            self.assertEqual(selected_entities(root), ["light.reading", "sensor.temperature"])
            path = root / "alexa_devices.yaml"
            path.write_text(path.read_text().replace("include_entities:", "exclude_entities:"))
            with self.assertRaisesRegex(ValueError, "Cannot read selected"):
                selected_entities(root)
            path.write_text("smart_home: {}\n")
            with self.assertRaisesRegex(ValueError, "manually managed"):
                selected_entities(root)


class DashboardTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_registry_authentication_out_of_order_responses_and_explicit_failure(self):
        _, registry = example_home()
        failure = False
        received = []

        async def websocket(request):
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            await socket.send_json({"type": "auth_required"})
            self.assertEqual(await socket.receive_json(), {"type": "auth", "access_token": "test-local"})
            await socket.send_json({"type": "auth_ok"})
            commands = [await socket.receive_json() for _ in range(4)]
            received.extend(command["type"] for command in commands)
            for command in reversed(commands):
                name = {"entity": "entities", "device": "devices", "area": "areas", "floor": "floors"}[
                    command["type"].split("/")[1].removesuffix("_registry")]
                await socket.send_json({"id": command["id"], "type": "result", "success": not failure,
                                        "result": registry[name],
                                        "error": {"code": "unauthorized"}})
            await socket.close()
            return socket

        app = web.Application()
        app.router.add_get("/api/websocket", websocket)
        server = TestServer(app)
        await server.start_server()
        self.addAsyncCleanup(server.close)
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {}, clear=True):
            connector = Connector({"ha_url": str(server.make_url("/")).rstrip("/"),
                                   "ha_token": "test-local"}, directory)
            try:
                async with aiohttp.ClientSession() as session:
                    connector.session = session
                    self.assertEqual(await connector.registries(), registry)
                    failure = True
                    with self.assertRaisesRegex(RuntimeError, "unauthorized"):
                        await connector.registries()
                self.assertEqual(set(received), {
                    "config/entity_registry/list", "config/device_registry/list",
                    "config/area_registry/list", "config/floor_registry/list"})
            finally:
                connector.db.close()

    async def test_dashboard_fallback_is_visible_and_local_routes_keep_trust_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            connector = Connector({"supervisor_ingress": True}, directory)
            self.addCleanup(connector.db.close)
            connector.trusted = Mock(return_value=True)
            connector.ha = AsyncMock(return_value=example_home()[0])
            connector.registries = AsyncMock(side_effect=RuntimeError("registry unavailable"))
            app = web.Application()
            app.router.add_get("/", connector.ui)
            app.router.add_get("/ui-state", connector.ui_state)
            app.router.add_get("/static/{name}", connector.static)
            client = TestClient(TestServer(app))
            await client.start_server()
            self.addAsyncCleanup(client.close)
            with patch("addon.connector.selected_entities", return_value=["light.reading"]):
                response = await client.get("/")
            page = await response.text()
            self.assertIn("entities are shown unassigned", page)
            self.assertTrue(Inputs(page).entities["light.reading"])
            self.assertIn("script-src 'self'", response.headers["Content-Security-Policy"])
            response = await client.get("/static/ui.css")
            self.assertEqual(response.status, 200)
            self.assertTrue(response.headers["Content-Type"].startswith("text/css"))
            self.assertEqual((await client.get("/static/connector.py")).status, 404)
            connector.links = [{"id": "link#test", "code": "1234", "client": "test-skill"}]
            response = await client.get("/ui-state")
            self.assertEqual((await response.json())["links"], connector.links)
            connector.trusted.return_value = False
            for path in ("/static/ui.js", "/ui-state"):
                self.assertEqual((await client.get(path)).status, 403)
