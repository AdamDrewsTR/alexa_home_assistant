"""Outbound-only local connector. Its approval UI must never be published."""

import argparse
import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

if __package__:
    from .ha_config import apply, generate
else:
    from ha_config import apply, generate

LOGGER = logging.getLogger("ha-alexa.connector")
MAX_MESSAGE = 28_000


class Connector:
    def __init__(self, config, data_dir):
        self.config = config
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.data_dir / "requests.sqlite")
        self.db.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, created REAL)")
        self.db.commit()
        self.links, self.grants = [], []
        self.ws = None
        self.csrf = secrets.token_urlsafe(32)
        self.session = None
        self.tasks = set()
        self.semaphore = asyncio.Semaphore(4)
        self.started = time.monotonic()
        self.last_poll = 0
        self.ready = False
        self.config_plan = None

    async def ha(self, method, path, body=None):
        if os.environ.get("SUPERVISOR_TOKEN"):
            url = "http://supervisor/core" + path
            token = os.environ["SUPERVISOR_TOKEN"]
        else:
            url = self.config["ha_url"].rstrip("/") + path
            token = self.config["ha_token"]
        for attempt in range(2):
            async with self.session.request(
                method, url, json=body, headers={"Authorization": "Bearer " + token},
                allow_redirects=False, timeout=aiohttp.ClientTimeout(total=3),
            ) as response:
                if response.status == 401 and not attempt and self.config.get("ha_refresh_token"):
                    await response.read()
                    async with self.session.post(
                        self.config["ha_url"].rstrip("/") + "/auth/token",
                        data={"grant_type": "refresh_token",
                              "refresh_token": self.config["ha_refresh_token"],
                              "client_id": self.config["ha_client_id"]},
                        allow_redirects=False, timeout=aiohttp.ClientTimeout(total=3),
                    ) as refreshed:
                        if refreshed.status != 200:
                            raise RuntimeError("HA local authorization expired; authorize the connector again")
                        auth = await refreshed.json()
                        self.config["ha_token"] = token = auth["access_token"]
                    continue
                if response.status != 200:
                    raise RuntimeError(f"Home Assistant HTTP {response.status}")
                limit = 4_000_000 if method == "GET" and path == "/api/states" else MAX_MESSAGE
                raw = bytearray()
                async for chunk in response.content.iter_chunked(8192):
                    raw.extend(chunk)
                    if len(raw) > limit:
                        raise RuntimeError("Home Assistant response exceeds the local size limit")
                return json.loads(raw)
        raise RuntimeError("HA authorization failed")

    async def supervisor(self, path):
        async with self.session.post(
            "http://supervisor" + path,
            headers={"Authorization": "Bearer " + os.environ["SUPERVISOR_TOKEN"]},
            allow_redirects=False, timeout=aiohttp.ClientTimeout(total=120),
        ) as response:
            if response.status != 200 or (await response.json()).get("result") != "ok":
                raise RuntimeError("Supervisor operation failed; inspect HA/Supervisor logs")

    async def dispatch(self, message, socket):
        async with self.semaphore:
            request_id = message.get("id", "")
            if (message.get("path") not in ("/api/alexa", "/api/alexa/smart_home")
                    or not isinstance(request_id, str) or len(request_id) != 32):
                LOGGER.warning("Rejected invalid cloud command")
                return
            self.db.execute("DELETE FROM seen WHERE created < ?", (time.time() - 86400,))
            try:
                # Commit before calling HA: an uncertain outcome is never replayed.
                self.db.execute("INSERT INTO seen VALUES (?, ?)", (request_id, time.time()))
                self.db.commit()
            except sqlite3.IntegrityError:
                self.db.rollback()
                LOGGER.warning("Duplicate command rejected")
                return
            try:
                event = message["event"]
                if not isinstance(event, dict):
                    raise ValueError("Invalid event")
                result = {"status": 200, "body": await self.ha("POST", message["path"], event)}
            except (aiohttp.ClientError, TimeoutError, ValueError, RuntimeError):
                LOGGER.error("HA command failed; check local HA logs and configuration")
                result = {"status": 502}
            encoded = json.dumps({"action": "response", "id": request_id, "body": result})
            if len(encoded.encode()) > MAX_MESSAGE:
                LOGGER.error("Response too large; reduce the exposed entity list")
                encoded = json.dumps({"action": "response", "id": request_id,
                                      "body": {"status": 413}})
            if not socket.closed:
                await socket.send_str(encoded)

    def completed(self, task):
        self.tasks.discard(task)
        if not task.cancelled() and task.exception():
            LOGGER.error("Command task interrupted; no automatic replay")

    async def connection(self):
        delay = 1
        while True:
            try:
                async with self.session.ws_connect(
                    self.config["websocket_url"],
                    headers={"Authorization": "Bearer " + self.config["connector_key"]},
                    heartbeat=60, max_msg_size=MAX_MESSAGE,
                ) as socket:
                    self.ws, self.ready = socket, True
                    LOGGER.info("Cloud relay connected")
                    delay = 1
                    poll = asyncio.create_task(self.poll(socket))
                    try:
                        async for message in socket:
                            if message.type == aiohttp.WSMsgType.TEXT:
                                body = json.loads(message.data)
                                if body.get("type") == "links":
                                    self.links = body.get("links", [])
                                    self.grants = body.get("grants", [])
                                    self.last_poll = time.monotonic()
                                elif body.get("type") == "request":
                                    if len(self.tasks) >= 4:
                                        LOGGER.warning("Connector busy; rejecting excess command")
                                        await socket.send_json({"action": "response", "id": body.get("id"),
                                                                "body": {"status": 429}})
                                    else:
                                        task = asyncio.create_task(self.dispatch(body, socket))
                                        self.tasks.add(task)
                                        task.add_done_callback(self.completed)
                            elif message.type == aiohttp.WSMsgType.ERROR:
                                raise ConnectionError("WebSocket interrupted")
                    finally:
                        poll.cancel()
                        await asyncio.gather(poll, return_exceptions=True)
            except (aiohttp.ClientError, ConnectionError, TimeoutError, ValueError):
                LOGGER.error("Cloud connection unavailable; reconnecting with backoff")
            finally:
                self.ready = False
                self.ws = None
                self.links, self.grants = [], []
            await asyncio.sleep(delay + secrets.randbelow(1000) / 1000)
            delay = min(delay * 2, 60)

    async def poll(self, socket):
        while not socket.closed:
            await socket.send_json({"action": "poll"})
            await asyncio.sleep(10)

    def trusted(self, request):
        peer = request.remote
        if not peer:
            return False
        if self.config.get("supervisor_ingress"):
            return peer == "172.30.32.2" and bool(request.headers.get("X-Ingress-Path"))
        return (ipaddress.ip_address(peer).is_loopback
                and urlsplit("//" + request.host).hostname in ("127.0.0.1", "localhost", "::1"))

    def fingerprints(self, files):
        directory = Path("/homeassistant")
        return {name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                if (directory / name).exists() else None for name in files}

    async def configuration(self, request, data):
        import html

        if not os.environ.get("SUPERVISOR_TOKEN") or not self.config.get("supervisor_ingress"):
            raise web.HTTPForbidden(text="Use configure-ha locally outside HA OS")
        action = data.get("action")
        try:
            if action == "preview":
                files = generate("/homeassistant", data.getall("entity", []),
                                 self.config.get("locale", "en-US"),
                                 self.config.get("custom_skill_id") or None,
                                 self.config.get("event_credentials") or None)
                self.config_plan = (files, self.fingerprints(files))
                content = "<h1>Review configuration</h1>"
                for name, value in files.items():
                    if self.config.get("event_credentials"):
                        value = value.replace(self.config["event_credentials"]["client_secret"], "[redacted]")
                    content += f"<h2>{html.escape(name)}</h2><pre>{html.escape(value)}</pre>"
                content += self.form("apply-config", "Back up and apply configuration")
                return web.Response(text=content, content_type="text/html",
                                    headers={"Cache-Control": "no-store"})
            if action == "apply-config":
                if not self.config_plan:
                    raise ValueError("Preview configuration first")
                files, fingerprints = self.config_plan
                if self.fingerprints(files) != fingerprints:
                    raise ValueError("Configuration changed since preview; preview it again")
                apply("/homeassistant", files)
                self.config_plan = None
                try:
                    await self.supervisor("/core/check")
                except (RuntimeError, aiohttp.ClientError, TimeoutError):
                    LOGGER.error("HA configuration check failed; backups retained, restart blocked")
                    return web.Response(status=409, text="HA check failed. Do not restart; restore the "
                                        ".alexa-backup files or inspect HA logs.")
                return web.Response(text="<h1>Configuration valid</h1>" +
                                    self.form("restart-ha", "Restart Home Assistant now"),
                                    content_type="text/html")
            if action == "restart-ha":
                # Always check again, even if files were changed after the preview.
                await self.supervisor("/core/check")
                await self.supervisor("/core/restart")
                return web.Response(text="Home Assistant restarted. Return to Alexa and link the skills.")
        except (ValueError, OSError, RuntimeError, aiohttp.ClientError, TimeoutError) as error:
            LOGGER.error("Local HA configuration operation failed")
            return web.Response(status=409, text=str(error))
        raise web.HTTPBadRequest()

    def form(self, action, label):
        return (f"<form method='post'><input type='hidden' name='csrf' value='{self.csrf}'>"
                f"<input type='hidden' name='action' value='{action}'>"
                f"<button>{label}</button></form>")

    async def ui(self, request):
        if not self.trusted(request):
            raise web.HTTPForbidden()
        if request.method == "POST":
            data = await request.post()
            if not hmac.compare_digest(data.get("csrf", ""), self.csrf):
                raise web.HTTPForbidden()
            if data.get("action") in ("preview", "apply-config", "restart-ha"):
                return await self.configuration(request, data)
            if not self.ws or self.ws.closed:
                raise web.HTTPServiceUnavailable(text="Relay disconnected; retry when connected")
            action = data.get("action")
            if action in ("approve", "revoke"):
                await self.ws.send_json({"action": action, "id": data.get("id", "")})
                return web.Response(text="Submitted. Return to Alexa to finish linking.")
            raise web.HTTPBadRequest()
        import html

        content = ["<h1>Alexa connector</h1>", "<p>Connected</p>" if self.ready else "<p>Disconnected</p>",
                   "<p>Only approve a code matching the page you just opened in Alexa.</p>"]
        for action, items in (("approve", self.links), ("revoke", self.grants)):
            for item in items:
                content.append(
                    f"<form method='post'><p>{html.escape(item.get('code', 'Linked account'))}"
                    f" — {html.escape(item['client'])}</p>"
                    f"<input type='hidden' name='csrf' value='{self.csrf}'>"
                    f"<input type='hidden' name='action' value='{action}'>"
                    f"<input type='hidden' name='id' value='{html.escape(item['id'], quote=True)}'>"
                    f"<button>{action.title()}</button></form>"
                )
        content.append("<p>Refresh to see new linking requests (up to ten seconds).</p>")
        if self.config.get("supervisor_ingress"):
            try:
                states = await self.ha("GET", "/api/states")
                content.append("<h2>Configure Home Assistant</h2><p>Select only entities Alexa should control.</p>"
                               f"<form method='post'><input type='hidden' name='csrf' value='{self.csrf}'>"
                               "<input type='hidden' name='action' value='preview'>")
                for state in states:
                    entity = state["entity_id"]
                    if entity.split(".")[0] in {
                        "light", "switch", "script", "scene", "climate", "fan", "cover",
                        "sensor", "binary_sensor", "media_player", "vacuum",
                    }:
                        content.append(
                            f"<label><input type='checkbox' name='entity' "
                            f"value='{html.escape(entity, quote=True)}'>{html.escape(entity)}</label><br>")
                content.append("<button>Preview configuration</button></form>")
            except (aiohttp.ClientError, RuntimeError, TimeoutError, ValueError):
                LOGGER.error("Could not load local HA entity selection")
                content.append("<p>Unable to load entities; check HA availability and app API permissions.</p>")
        return web.Response(text="<!doctype html><meta charset='utf-8'>" + "".join(content),
                            content_type="text/html",
                            headers={"Cache-Control": "no-store", "X-Frame-Options": "SAMEORIGIN",
                                     "Content-Security-Policy": "default-src 'none'; form-action 'self'; base-uri 'none'"})

    async def health(self, request):
        return web.json_response({"alive": True, "connected": self.ready,
                                  "poll_age_seconds": int(time.monotonic() - self.last_poll)
                                  if self.last_poll else None})

    async def start(self):
        self.session = aiohttp.ClientSession()
        app = web.Application(client_max_size=8192)
        app.router.add_route("*", "/", self.ui)
        app.router.add_get("/health", self.health)
        runner = web.AppRunner(app)
        await runner.setup()
        host = "0.0.0.0" if self.config.get("supervisor_ingress") else "127.0.0.1"
        site = web.TCPSite(runner, host, self.config.get("ui_port", 8099))
        try:
            await site.start()
            await self.connection()
        finally:
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            await runner.cleanup()
            await self.session.close()
            self.db.close()


def load_config(path):
    config = json.loads(Path(path).read_text())
    url = urlsplit(config["websocket_url"])
    if url.scheme != "wss" or not url.hostname or url.username or url.password or url.query:
        raise ValueError("Connector requires a wss:// endpoint without embedded credentials")
    if not isinstance(config.get("connector_key"), str) or len(config["connector_key"]) < 32:
        raise ValueError("Connector key must be at least 32 characters")
    if not os.environ.get("SUPERVISOR_TOKEN") and not (
        config.get("ha_url") and config.get("ha_token")
    ):
        raise ValueError("Outside HA OS provide ha_url and a local-only ha_token")
    return config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/data/options.json")
    parser.add_argument("--data-dir", default="/data")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(Connector(load_config(args.config), args.data_dir).start())
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f"Connector configuration error: {error}\n")


if __name__ == "__main__":
    main()
