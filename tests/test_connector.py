import asyncio
import json
import tempfile
import time
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
        self.connector.registries = AsyncMock(return_value={"entities": [], "devices": [], "areas": [], "floors": []})
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
        self.assertEqual(json.loads(response["data"])["status"], 200)
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

    async def test_phone_dispatch_is_locally_scoped_and_expires(self):
        webhook = "a" * 64
        phone_id = "b" * 64
        self.connector.phones[phone_id] = {"name": "Phone", "webhook_id": webhook}
        self.connector.ha = AsyncMock(return_value={})
        socket = AsyncMock()
        socket.closed = False
        payload = {"type": "update_location", "encrypted": True, "encrypted_data": "opaque"}
        request = {"id": "c" * 32, "phone": phone_id, "event": payload,
                   "expires": int(time.time()) + 10, "path": "/api/services/lock/unlock"}
        await self.connector.dispatch(request, socket)
        self.connector.ha.assert_awaited_once_with("POST", "/api/webhook/" + webhook, payload)
        self.assertIn(phone_id, self.connector.phone_updates)
        self.connector.ha.reset_mock()
        for index, overrides in enumerate((
            {"phone": "unenrolled"},
            {"event": {"type": "call_service"}},
            {"event": {"type": []}},
            {"expires": int(time.time()) - 1},
        )):
            socket.send_str.reset_mock()
            await self.connector.dispatch({**request, **overrides, "id": str(index) * 32}, socket)
            self.assertEqual(json.loads(json.loads(socket.send_str.call_args.args[0])["data"])["status"], 403)
        self.connector.ha.assert_not_awaited()
        self.connector.phones.clear()
        await self.connector.dispatch({**request, "id": "d" * 32}, socket)
        self.connector.ha.assert_not_awaited()

    async def test_multiple_phones_route_independently_and_revoking_one_keeps_the_other(self):
        socket = AsyncMock()
        socket.closed = False
        self.connector.ha = AsyncMock(return_value={})
        for index in ("a", "b"):
            self.connector.phones[index * 64] = {"name": index, "webhook_id": index * 64}
            await self.connector.dispatch({
                "id": index * 32, "phone": index * 64, "expires": int(time.time()) + 10,
                "event": {"type": "update_location", "data": {"location_name": index}},
            }, socket)
        self.assertEqual([call.args[1] for call in self.connector.ha.await_args_list],
                         ["/api/webhook/" + "a" * 64, "/api/webhook/" + "b" * 64])
        self.connector.phones.pop("a" * 64)
        self.connector.ha.reset_mock()
        for index, phone in (("c", "a"), ("d", "b")):
            await self.connector.dispatch({
                "id": index * 32, "phone": phone * 64, "expires": int(time.time()) + 10,
                "event": {"type": "update_location"},
            }, socket)
        self.connector.ha.assert_awaited_once_with("POST", "/api/webhook/" + "b" * 64,
                                                  {"type": "update_location"})

    async def test_phone_enrollment_ui_persists_revokes_and_enforces_csrf(self):
        self.connector.ha = AsyncMock(return_value=[{"name": "My phone", "webhook_id": "a" * 64}])
        socket = AsyncMock()
        socket.closed = False
        self.connector.ws = socket
        app = web.Application()
        app.router.add_route("*", "/phones", self.connector.phones_ui)
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        response = await client.get("/phones")
        self.assertIn("Enroll My phone", await response.text())
        form = {"action": "enroll", "name": "My phone", "webhook_id": "a" * 64}
        response = await client.post("/phones", data=form)
        self.assertEqual(response.status, 403)
        socket.send_json.assert_not_awaited()
        response = await client.post("/phones", data={**form, "csrf": self.connector.csrf})
        self.assertEqual(response.status, 200)
        self.assertEqual(len(self.connector.phones), 1)
        self.assertEqual(self.connector.phones_path.stat().st_mode & 0o777, 0o600)
        key = next(iter(self.connector.phones))
        socket.send_json.assert_awaited_once_with({"action": "poll", "phones": [key]})
        restarted = Connector({}, self.temp.name)
        try:
            self.assertEqual(restarted.phones, self.connector.phones)
            self.assertNotIn("a" * 64, restarted.redact("request /api/webhook/" + "a" * 64))
        finally:
            restarted.db.close()
        response = await client.post("/phones", data={"action": "remove", "id": key,
                                                      "csrf": self.connector.csrf})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.connector.phones, {})
        self.assertEqual(json.loads(self.connector.phones_path.read_text()), {})
        response = await client.post("/phones", data={**form, "webhook_id": "../auth",
                                                      "csrf": self.connector.csrf})
        self.assertEqual(response.status, 400)
        response = await client.post("/phones", data={**form, "webhook_id": "b" * 64,
                                                      "csrf": self.connector.csrf})
        self.assertEqual(response.status, 400)
        self.assertEqual(self.connector.phones, {})

    async def test_old_domain_saved_preview_cannot_be_applied_after_upgrade(self):
        self.connector.plan_path.write_text(json.dumps({
            "files": {"alexa_devices.yaml": "old configuration"}, "applied": True,
        }))
        with self.assertLogs("ha-alexa.connector", level="WARNING"):
            restarted = Connector({}, self.temp.name)
        try:
            self.assertIsNone(restarted.config_plan)
            self.assertTrue(restarted.plan_path.exists())
        finally:
            restarted.db.close()

    async def test_discovery_response_fragments_stay_bounded_and_reassemble_without_loss(self):
        socket = AsyncMock()
        socket.closed = False
        result = {"event": {"payload": {"endpoints": [
            {"endpointId": f"device-{index}", "friendlyName": "Kitchen \u2600 \"light\"",
             "capabilities": ["Alexa.PowerController"] * 12} for index in range(180)
        ]}}}
        self.connector.ha = AsyncMock(return_value=result)
        await self.connector.dispatch(
            {"id": "b" * 32, "path": "/api/alexa/smart_home", "event": {}}, socket)
        frames = [json.loads(call.args[0]) for call in socket.send_str.call_args_list]
        self.assertGreater(len(frames), 1)
        self.assertTrue(all(len(call.args[0].encode()) <= 28_000
                            for call in socket.send_str.call_args_list))
        self.assertEqual(json.loads("".join(frame["data"] for frame in frames)),
                         {"status": 200, "body": result})
        self.assertEqual([frame["index"] for frame in frames], list(range(len(frames))))
        self.assertTrue(all(frame["total"] == len(frames) for frame in frames))

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
             patch("addon.connector.apply", return_value="backup-stamp") as apply:
            response = await self.connector.configuration(None, MultiDict(action="preview", entity="light.test"))
            self.assertEqual(response.status, 200)
            plan = self.connector.config_plan["id"]
            self.connector.fingerprints.return_value = {"configuration.yaml": "changed"}
            response = await self.connector.configuration(None, MultiDict(action="apply-config", plan=plan))
            self.assertEqual(response.status, 409)
            apply.assert_not_called()
            await self.connector.configuration(None, MultiDict(action="preview", entity="light.test"))
            plan = self.connector.config_plan["id"]
            response = await self.connector.configuration(None, MultiDict(action="apply-config", plan=plan))
            self.assertEqual(response.status, 200)
            apply.assert_called_once_with("/homeassistant", files)
            self.connector.supervisor.assert_awaited_once_with("/core/check")
            self.connector.supervisor.reset_mock()
            self.connector.supervisor.side_effect = RuntimeError("Configuration invalid")
            response = await self.connector.configuration(None, MultiDict(action="restart-ha"))
            self.assertEqual(response.status, 409)
            self.connector.supervisor.assert_awaited_once_with("/core/check")

    async def test_failed_check_preserves_preview_across_restart_and_retries_without_reapplying(self):
        self.connector.config.update(supervisor_ingress=True,
                                     event_credentials={"client_secret": "private-event-secret"})
        self.connector.supervisor = AsyncMock(
            side_effect=RuntimeError("Invalid YAML: private-event-secret at alexa_bridge.yaml"))
        self.connector.fingerprints = Mock(return_value={"configuration.yaml": "unchanged"})
        files = {"configuration.yaml": "alexa: !include alexa_bridge.yaml\n"}
        with patch.dict("os.environ", {"SUPERVISOR_TOKEN": "local-only"}), \
             patch("addon.connector.generate", return_value=files), \
             patch("addon.connector.apply", return_value="backup-stamp") as apply:
            await self.connector.configuration(None, MultiDict(action="preview", entity="light.test"))
            plan = self.connector.config_plan["id"]
            with self.assertLogs("ha-alexa.connector", level="ERROR") as logs:
                response = await self.connector.configuration(
                    None, MultiDict(action="apply-config", plan=plan))
            self.assertEqual(response.status, 409)
            self.assertIn("Invalid YAML", response.text)
            self.assertIn("backup-stamp", response.text)
            self.assertNotIn("private-event-secret", response.text + str(logs.output))
            self.assertEqual(self.connector.plan_path.stat().st_mode & 0o777, 0o600)
            restarted = Connector(self.connector.config, self.temp.name)
            try:
                restarted.supervisor = AsyncMock()
                restarted.fingerprints = self.connector.fingerprints
                response = await restarted.configuration(
                    None, MultiDict(action="apply-config", plan=plan))
                self.assertEqual(response.status, 200)
                apply.assert_called_once()
                restarted.supervisor.assert_awaited_once_with("/core/check")
                response = await restarted.configuration(
                    None, MultiDict(action="apply-config", plan="stale-preview"))
                self.assertEqual(response.status, 409)
                self.assertIn("replaced", response.text)
            finally:
                restarted.db.close()

    async def test_supervisor_check_surfaces_redacted_response_message(self):
        self.connector.config["event_credentials"] = {"client_secret": "private-event-secret"}
        response = AsyncMock()
        response.status = 400
        response.text.return_value = json.dumps({
            "result": "error", "message": "Invalid config: private-event-secret in alexa_bridge.yaml"})
        context = AsyncMock()
        context.__aenter__.return_value = response
        self.connector.session = Mock()
        self.connector.session.post.return_value = context
        with patch.dict("os.environ", {"SUPERVISOR_TOKEN": "local-only"}):
            with self.assertRaises(RuntimeError) as stopped:
                await self.connector.supervisor("/core/check")
        message = str(stopped.exception)
        self.assertIn("/core/check HTTP 400", message)
        self.assertIn("alexa_bridge.yaml", message)
        self.assertNotIn("private-event-secret", message)

    async def test_double_submission_does_not_apply_again_while_check_is_running(self):
        self.connector.config["supervisor_ingress"] = True
        started, finish = asyncio.Event(), asyncio.Event()

        async def check(path):
            started.set()
            await finish.wait()

        self.connector.supervisor = AsyncMock(side_effect=check)
        self.connector.fingerprints = Mock(return_value={"configuration.yaml": "unchanged"})
        with patch.dict("os.environ", {"SUPERVISOR_TOKEN": "local-only"}), \
             patch("addon.connector.generate", return_value={"configuration.yaml": "alexa: {}\n"}), \
             patch("addon.connector.apply", return_value="backup-stamp") as apply:
            await self.connector.configuration(None, MultiDict(action="preview", entity="light.test"))
            data = MultiDict(action="apply-config", plan=self.connector.config_plan["id"])
            first = asyncio.create_task(self.connector.configuration(None, data))
            try:
                await asyncio.wait_for(started.wait(), 1)
                second = await self.connector.configuration(None, data)
                self.assertEqual(second.status, 409)
                self.assertIn("in progress", second.text)
            finally:
                finish.set()
                response = await first
            self.assertEqual(response.status, 200)
            apply.assert_called_once()
            self.connector.supervisor.assert_awaited_once_with("/core/check")
