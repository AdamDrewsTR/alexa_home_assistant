import asyncio
import json
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from addon.connector import Connector


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_phone_delivery_to_local_ha_preserves_encryption_and_revocation(self):
        webhook = "a" * 64
        phone_id = "b" * 64
        payload = {"type": "update_location", "encrypted": True, "encrypted_data": "opaque"}
        calls = []

        async def ha(request):
            calls.append(await request.json())
            return web.json_response({}, status=201 if calls[-1]["type"] == "register_sensor" else 200)

        ha_app = web.Application()
        ha_app.router.add_post("/api/webhook/" + webhook, ha)
        ha_server = TestServer(ha_app)
        await ha_server.start_server()
        self.addAsyncCleanup(ha_server.close)
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {}, clear=True):
            connector = Connector({
                "ha_url": str(ha_server.make_url("/")).rstrip("/"), "ha_token": "local-token",
            }, directory)
            connector.phones[phone_id] = {"name": "Phone", "webhook_id": webhook}
            socket = AsyncMock()
            socket.closed = False
            try:
                async with aiohttp.ClientSession() as session:
                    connector.session = session
                    await connector.dispatch({"id": "e" * 32, "phone": phone_id, "event": payload,
                                              "expires": int(time.time()) + 10}, socket)
                    self.assertEqual(calls, [payload])
                    self.assertEqual(json.loads(json.loads(socket.send_str.call_args.args[0])["data"]),
                                     {"status": 200, "body": {}})
                    await connector.dispatch({
                        "id": "c" * 32, "phone": phone_id, "event": {"type": "register_sensor", "data": {}},
                        "expires": int(time.time()) + 10,
                    }, socket)
                    self.assertEqual(json.loads(json.loads(socket.send_str.call_args.args[0])["data"])["status"], 200)
                    connector.phones.clear()
                    await connector.dispatch({"id": "f" * 32, "phone": phone_id, "event": payload,
                                              "expires": int(time.time()) + 10}, socket)
                    self.assertEqual(len(calls), 2)
                    self.assertEqual(json.loads(json.loads(socket.send_str.call_args.args[0])["data"])["status"], 403)
            finally:
                connector.db.close()

    async def test_expired_ha_token_is_refreshed_locally_and_oversize_reply_is_explicit(self):
        events, refreshes = [], []

        async def ha(request):
            events.append(request.headers["Authorization"])
            if request.headers["Authorization"] == "Bearer expired":
                return web.Response(status=401)
            return web.json_response({"data": "x" * 255_980})

        async def refresh(request):
            refreshes.append(dict(await request.post()))
            return web.json_response({"access_token": "fresh"})

        app = web.Application()
        app.router.add_post("/api/alexa/smart_home", ha)
        app.router.add_post("/auth/token", refresh)
        server = TestServer(app)
        await server.start_server()
        self.addAsyncCleanup(server.close)
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {}, clear=True):
            connector = Connector({
                "ha_url": str(server.make_url("/")).rstrip("/"), "ha_token": "expired",
                "ha_refresh_token": "local-refresh", "ha_client_id": "local-client",
            }, directory)
            socket = AsyncMock()
            socket.closed = False
            try:
                async with aiohttp.ClientSession() as session:
                    connector.session = session
                    await connector.dispatch({"id": "d" * 32, "path": "/api/alexa/smart_home",
                                              "event": {"directive": {}}}, socket)
                self.assertEqual(events, ["Bearer expired", "Bearer fresh"])
                self.assertEqual(refreshes, [{"grant_type": "refresh_token",
                                              "refresh_token": "local-refresh",
                                              "client_id": "local-client"}])
                response = json.loads("".join(json.loads(call.args[0])["data"]
                                              for call in socket.send_str.call_args_list))
                self.assertEqual(response["status"], 413)
                self.assertEqual(connector.config["ha_token"], "fresh")
            finally:
                connector.db.close()

    async def test_real_websocket_to_local_ha_roundtrip_and_reconnect(self):
        received = asyncio.Queue()
        connections = []
        calls = []

        async def ha(request):
            self.assertEqual(request.headers["Authorization"], "Bearer local-only-token")
            calls.append(await request.json())
            return web.json_response({"event": {"payload": {"powerState": "ON"}}})

        ha_app = web.Application()
        ha_app.router.add_post("/api/alexa/smart_home", ha)
        ha_server = TestServer(ha_app)
        await ha_server.start_server()
        self.addAsyncCleanup(ha_server.close)

        async def cloud(request):
            self.assertEqual(request.headers["Authorization"], "Bearer connector-test-key")
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            connections.append(socket)
            await socket.send_json({"type": "request", "id": "b" * 32,
                                    "path": "/api/alexa/smart_home",
                                    "event": {"directive": {"header": {"messageId": "test"}}}})
            async for message in socket:
                if message.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(message.data)
                    if data.get("action") == "response":
                        await received.put(data)
                    elif data.get("action") == "poll":
                        await socket.send_json({"type": "links", "links": [], "grants": []})
            return socket

        cloud_app = web.Application()
        cloud_app.router.add_get("/connector", cloud)
        cloud_server = TestServer(cloud_app)
        await cloud_server.start_server()
        self.addAsyncCleanup(cloud_server.close)
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {}, clear=True):
            connector = Connector({
                "websocket_url": str(cloud_server.make_url("/connector")).replace("http:", "ws:"),
                "connector_key": "connector-test-key",
                "ha_url": str(ha_server.make_url("/")).rstrip("/"),
                "ha_token": "local-only-token",
            }, directory)
            async with aiohttp.ClientSession() as session:
                connector.session = session
                task = asyncio.create_task(connector.connection())
                try:
                    result = await asyncio.wait_for(received.get(), 5)
                    self.assertEqual(json.loads(result["data"])["body"]["event"]["payload"]["powerState"], "ON")
                    await connections[0].close()
                    deadline = asyncio.get_running_loop().time() + 5
                    while len(connections) < 2 and asyncio.get_running_loop().time() < deadline:
                        await asyncio.sleep(0.05)
                    self.assertGreaterEqual(len(connections), 2)
                    await asyncio.sleep(0.1)
                    self.assertEqual(len(calls), 1)  # Same command is not replayed after reconnect.
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    await asyncio.gather(*connector.tasks, return_exceptions=True)
                    connector.db.close()
