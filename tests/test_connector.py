import json
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from multidict import MultiDict

from addon.connector import Connector


class ConnectorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.connector = Connector({}, self.temp.name)
        self.addCleanup(self.connector.db.close)

    async def test_command_forwarding_and_persistent_duplicate_protection(self):
        socket = AsyncMock()
        socket.closed = False
        self.connector.ha = AsyncMock(return_value={"event": {"payload": {"ok": True}}})
        request = {"type": "request", "id": "a" * 32, "path": "/api/alexa/smart_home",
                   "event": {"directive": {}}}
        await self.connector.dispatch(request, socket)
        await self.connector.dispatch(request, socket)
        self.connector.ha.assert_awaited_once_with("POST", "/api/alexa/smart_home", request["event"])
        response = json.loads(socket.send_str.call_args.args[0])
        self.assertEqual(response["body"]["status"], 200)
        # A restarted connector must also refuse the uncertain duplicate.
        restarted = Connector({}, self.temp.name)
        try:
            restarted.ha = AsyncMock()
            await restarted.dispatch(request, socket)
            restarted.ha.assert_not_awaited()
        finally:
            restarted.db.close()

    async def test_arbitrary_routes_are_not_a_proxy(self):
        self.connector.ha = AsyncMock()
        await self.connector.dispatch({"id": "a" * 32, "path": "/api/services/lock/unlock"}, AsyncMock())
        self.connector.ha.assert_not_awaited()

    async def test_local_ui_requires_trusted_source_and_csrf(self):
        request = make_mocked_request("POST", "/", headers={})
        self.assertFalse(self.connector.trusted(request))
        self.connector.config["supervisor_ingress"] = True
        self.assertFalse(self.connector.trusted(request))

    async def test_real_local_approval_ui_enforces_host_csrf_and_ingress_peer(self):
        app = web.Application()
        app.router.add_route("*", "/", self.connector.ui)
        app.router.add_get("/health", self.connector.health)
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        socket = AsyncMock()
        socket.closed = False
        self.connector.ws = socket
        self.connector.links = [{"code": "12345678", "client": "skill", "id": "link#id"}]
        response = await client.get("/")
        self.assertEqual(response.status, 200)
        self.assertIn(self.connector.csrf, await response.text())
        response = await client.post("/", data={"action": "approve", "id": "link#id", "csrf": "wrong"})
        self.assertEqual(response.status, 403)
        socket.send_json.assert_not_awaited()
        response = await client.post("/", data={"action": "approve", "id": "link#id",
                                                "csrf": self.connector.csrf})
        self.assertEqual(response.status, 200)
        socket.send_json.assert_awaited_once_with({"action": "approve", "id": "link#id"})
        response = await client.get("/", headers={"Host": "untrusted.example"})
        self.assertEqual(response.status, 403)
        self.connector.config["supervisor_ingress"] = True
        response = await client.get("/", headers={"X-Ingress-Path": "/fake"})
        self.assertEqual(response.status, 403)
        response = await client.get("/health")
        self.assertEqual(response.status, 200)  # Cloud outages must not trigger watchdog restarts.
        self.assertFalse((await response.json())["connected"])

    async def test_config_preview_checks_concurrent_edits_and_restart_rechecks_ha(self):
        self.connector.config["supervisor_ingress"] = True
        self.connector.supervisor = AsyncMock()
        self.connector.fingerprints = Mock(return_value={"configuration.yaml": "before"})
        files = {"configuration.yaml": "alexa: !include alexa_bridge.yaml\n"}
        with patch.dict("os.environ", {"SUPERVISOR_TOKEN": "local-only"}), \
             patch("addon.connector.generate", return_value=files), \
             patch("addon.connector.apply") as apply:
            response = await self.connector.configuration(None, MultiDict(action="preview", entity="light.test"))
            self.assertEqual(response.status, 200)
            self.connector.fingerprints.return_value = {"configuration.yaml": "changed"}
            response = await self.connector.configuration(None, MultiDict(action="apply-config"))
            self.assertEqual(response.status, 409)
            apply.assert_not_called()
            await self.connector.configuration(None, MultiDict(action="preview", entity="light.test"))
            response = await self.connector.configuration(None, MultiDict(action="apply-config"))
            self.assertEqual(response.status, 200)
            apply.assert_called_once_with("/homeassistant", files)
            self.connector.supervisor.assert_awaited_once_with("/core/check")
            self.connector.supervisor.reset_mock()
            self.connector.supervisor.side_effect = RuntimeError("Configuration invalid")
            response = await self.connector.configuration(None, MultiDict(action="restart-ha"))
            self.assertEqual(response.status, 409)
            self.connector.supervisor.assert_awaited_once_with("/core/check")
