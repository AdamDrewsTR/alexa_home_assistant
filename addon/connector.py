"""Outbound-only local connector. Its approval UI must never be published."""

import argparse
import asyncio
import hashlib
import hmac
import html
import ipaddress
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

if __package__:
    from .ha_config import apply, atomic_write, generate, selected_entities
    from .ui import dashboard, document
else:
    from ha_config import apply, atomic_write, generate, selected_entities
    from ui import dashboard, document

LOGGER = logging.getLogger("ha-alexa.connector")
MAX_MESSAGE = 28_000
MAX_RESPONSE = 256_000
RESPONSE_CHUNK = 12_000
PHONE_TYPES = {"update_location", "get_config", "get_zones", "update_registration",
               "register_sensor", "update_sensor_states"}


class HAError(RuntimeError):
    def __init__(self, message, status):
        super().__init__(message)
        self.status = status


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
        self.plan_path = self.data_dir / "configuration-plan.json"
        self.config_plan = json.loads(self.plan_path.read_text()) if self.plan_path.exists() else None
        if self.config_plan and "alexa_devices.yaml" in self.config_plan["files"]:
            LOGGER.warning("Saved preview uses the retired alexa_devices domain; create a new configuration preview")
            self.config_plan = None
        self.configuration_lock = asyncio.Lock()
        self.phones_path = self.data_dir / "phones.json"
        self.phones = json.loads(self.phones_path.read_text()) if self.phones_path.exists() else {}
        self.synced_phones = []
        self.phone_relay_url = ""
        self.phone_updates = {}

    def redact(self, text):
        for value in (
            self.config.get("connector_key"), self.config.get("ha_token"),
            self.config.get("ha_refresh_token"), os.environ.get("SUPERVISOR_TOKEN"),
            (self.config.get("event_credentials") or {}).get("client_secret"),
        ):
            if value:
                text = text.replace(value, "[redacted]")
        for phone in self.phones.values():
            text = text.replace(phone["webhook_id"], "[redacted]")
        return text

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
                if response.status != 200 and not (
                    response.status == 201 and method == "POST" and path.startswith("/api/webhook/")
                ):
                    hint = ("; Alexa endpoint is not loaded: validate configuration and restart HA"
                            if response.status == 404 and path in ("/api/alexa", "/api/alexa/smart_home")
                            else "")
                    raise HAError(f"Home Assistant {method} {path} HTTP {response.status}{hint}", response.status)
                limit = 4_000_000 if method == "GET" and path in ("/api/states", "/api/alexa_device_endpoints") else MAX_RESPONSE
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
            raw = await response.text()
            try:
                result = json.loads(raw)
            except ValueError:
                result = {}
            if not isinstance(result, dict):
                raise RuntimeError(f"Supervisor {path} HTTP {response.status}: invalid response")
            if response.status != 200 or result.get("result") != "ok":
                detail = result.get("message") or "No diagnostic message returned"
                raise RuntimeError(self.redact(
                    f"Supervisor {path} HTTP {response.status}: {detail}"))

    async def registries(self):
        if os.environ.get("SUPERVISOR_TOKEN"):
            url, token = "http://supervisor/core/api/websocket", os.environ["SUPERVISOR_TOKEN"]
        else:
            url, token = self.config["ha_url"].rstrip("/") + "/api/websocket", self.config["ha_token"]
        commands = {"entities": "config/entity_registry/list", "devices": "config/device_registry/list",
                    "areas": "config/area_registry/list", "floors": "config/floor_registry/list"}
        async with asyncio.timeout(12):
            async with self.session.ws_connect(url, max_msg_size=4_000_000) as socket:
                if (await socket.receive_json()).get("type") != "auth_required":
                    raise RuntimeError("Unexpected HA registry authentication response")
                await socket.send_json({"type": "auth", "access_token": token})
                if (await socket.receive_json()).get("type") != "auth_ok":
                    raise RuntimeError("HA registry authorization failed; check app permissions")
                pending = {}
                for request_id, (name, command) in enumerate(commands.items(), start=1):
                    pending[request_id] = name
                    await socket.send_json({"id": request_id, "type": command})
                result = {}
                while pending:
                    response = await socket.receive_json()
                    name = pending.pop(response.get("id"), None)
                    if name is None:
                        raise RuntimeError("Unexpected HA registry response")
                    if not response.get("success") or not isinstance(response.get("result"), list):
                        code = (response.get("error") or {}).get("code", "invalid_response")
                        raise RuntimeError(f"Cannot read HA {name} registry ({code})")
                    result[name] = response["result"]
                return result

    def page(self, content, status=200):
        return web.Response(
            status=status, text=document("<main class='result-page'>" + content +
                                         "<p><a href='./'>Back to your home</a></p></main>", self.csrf),
            content_type="text/html", headers=self.page_headers(),
        )

    @staticmethod
    def page_headers():
        return {"Cache-Control": "no-store", "X-Frame-Options": "SAMEORIGIN",
                "Content-Security-Policy": "default-src 'none'; style-src 'self'; script-src 'self'; "
                "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'self'"}

    async def dispatch(self, message, socket):
        async with self.semaphore:
            request_id = message.get("id", "")
            phone_id = message.get("phone")
            phone = self.phones.get(phone_id) if isinstance(phone_id, str) else None
            is_phone = "phone" in message
            if ((not is_phone and message.get("path") not in ("/api/alexa", "/api/alexa/smart_home"))
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
                if is_phone:
                    if (not phone or not isinstance(event.get("type"), str) or event["type"] not in PHONE_TYPES
                            or len(json.dumps(event).encode()) > 20_000
                            or type(message.get("expires")) is not int
                            or not time.time() < message["expires"] <= time.time() + 15):
                        result = {"status": 403}
                        LOGGER.warning("Rejected unenrolled, expired, or unsupported phone request")
                    else:
                        result = {"status": 200, "body": await self.ha(
                            "POST", "/api/webhook/" + phone["webhook_id"], event)}
                        if event["type"] == "update_location":
                            self.phone_updates[message["phone"]] = time.time()
                else:
                    result = {"status": 200, "body": await self.ha("POST", message["path"], event)}
            except (aiohttp.ClientError, TimeoutError, ValueError, RuntimeError) as error:
                detail = self.redact(str(error)) or type(error).__name__
                if is_phone and phone:
                    detail = detail.replace(phone["webhook_id"], "[redacted]")
                LOGGER.error("HA command %s failed: %s", "phone" if is_phone else message["path"], detail)
                result = {"status": 502}
            raw = json.dumps(result)
            if len(raw) > MAX_RESPONSE:
                LOGGER.error("HA response exceeds Alexa's response-size limit")
                raw = json.dumps({"status": 413})
            for index, offset in enumerate(range(0, len(raw), RESPONSE_CHUNK)):
                if socket.closed:
                    break
                await socket.send_str(json.dumps({
                    "action": "response", "id": request_id, "index": index,
                    "total": (len(raw) + RESPONSE_CHUNK - 1) // RESPONSE_CHUNK,
                    "data": raw[offset:offset + RESPONSE_CHUNK],
                }))

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
                                    self.synced_phones = body.get("phones", [])
                                    self.phone_relay_url = body.get("phone_relay_url", "")
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
                self.synced_phones = []
            await asyncio.sleep(delay + secrets.randbelow(1000) / 1000)
            delay = min(delay * 2, 60)

    async def poll(self, socket):
        while not socket.closed:
            await socket.send_json({"action": "poll", "phones": list(self.phones)})
            await asyncio.sleep(10)

    async def phones_ui(self, request):
        if not self.trusted(request):
            raise web.HTTPForbidden()
        if request.method == "POST":
            data = await request.post()
            if not hmac.compare_digest(data.get("csrf", ""), self.csrf):
                raise web.HTTPForbidden()
            phones = dict(self.phones)
            if data.get("action") == "enroll":
                webhook = data.get("webhook_id", "").strip()
                name = data.get("name", "").strip()
                if not re.fullmatch(r"[a-f0-9]{64}", webhook) or not 1 <= len(name) <= 80:
                    raise web.HTTPBadRequest(text="Provide a phone name and its 64-character HA webhook ID.")
                try:
                    candidates = await self.ha("GET", "/api/alexa_device_endpoints/phones")
                except (aiohttp.ClientError, TimeoutError, ValueError, RuntimeError) as error:
                    LOGGER.warning("Phone enrollment validation unavailable: %s", self.redact(str(error)))
                    raise web.HTTPServiceUnavailable(
                        text="Cannot verify phone registration. Update/apply the local integration and restart HA.") from error
                if not any(candidate["webhook_id"] == webhook for candidate in candidates):
                    raise web.HTTPBadRequest(text="This phone is not registered with HA. Register it locally first.")
                key = hashlib.sha256(webhook.encode()).hexdigest()
                if key not in phones and len(phones) >= 20:
                    raise web.HTTPBadRequest(text="At most 20 phones may be enrolled.")
                phones[key] = {"name": name, "webhook_id": webhook}
            elif data.get("action") == "remove":
                phones.pop(data.get("id", ""), None)
            else:
                raise web.HTTPBadRequest()
            atomic_write(self.phones_path, json.dumps(phones))
            self.phones = phones
            if self.ws and not self.ws.closed:
                await self.ws.send_json({"action": "poll", "phones": list(phones)})
            raise web.HTTPSeeOther(location="phones")
        content = ("<h1>Companion phone location relay</h1>"
                   "<p>Experimental: location and phone sensor sync only; no remote dashboards or service calls. "
                   "Register the official Companion app with HA on home Wi-Fi first. "
                   "Keep its internal URL and home Wi-Fi SSID configured. Set its external URL to:</p>")
        content += "<pre>" + html.escape(self.phone_relay_url or "Waiting for updated cloud relay") + "</pre>"
        content += ("<p>Enable Always/background location permission. Associate the existing phone tracker with "
                    "your person in HA. Test on cellular with the phone locked before relying on automations. "
                    "A missed update leaves the last known location; it does not mean away.</p>")
        for key, phone in self.phones.items():
            status = "Cloud enrolled" if self.ready and key in self.synced_phones else "Waiting for cloud sync"
            updated = self.phone_updates.get(key)
            age = f"; last location accepted {int(time.time() - updated)} seconds ago" if updated else "; no location received this session"
            content += ("<h2>" + html.escape(phone["name"]) + "</h2><p>" + status + age + "</p>"
                        f"<form method='post'><input type='hidden' name='csrf' value='{self.csrf}'>"
                        f"<input type='hidden' name='id' value='{key}'>"
                        "<input type='hidden' name='action' value='remove'>"
                        "<button>Revoke phone</button></form>")
        content += ("<h2>Enroll a phone</h2><p>The list below contains phones already registered with HA's "
                    "mobile_app integration. Enroll only devices whose location you have permission to share. "
                    "Each phone must grant its own background location permission.</p>")
        try:
            candidates = await self.ha("GET", "/api/alexa_device_endpoints/phones")
            for candidate in candidates:
                content += (f"<form method='post'><input type='hidden' name='csrf' value='{self.csrf}'>"
                            f"<input type='hidden' name='webhook_id' value='{html.escape(candidate['webhook_id'], quote=True)}'>"
                            f"<input type='hidden' name='name' value='{html.escape(candidate['name'], quote=True)}'>"
                            "<input type='hidden' name='action' value='enroll'>"
                            "<button>Enroll " + html.escape(candidate["name"]) +
                            "</button></form>")
        except (aiohttp.ClientError, TimeoutError, ValueError, RuntimeError) as error:
            LOGGER.warning("Phone enrollment list unavailable: %s", self.redact(str(error)))
            content += "<p>Phone list unavailable. Update/apply the local integration and restart HA.</p>"
        content += ("<p>Revocation takes effect locally immediately. Cloud enrollment expires within two minutes "
                    "without connector polling. Re-registering the app requires enrolling its new webhook.</p>")
        return self.page(content)

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
        if self.configuration_lock.locked():
            return web.Response(status=409, text="A configuration operation is in progress. Wait for it to finish.")
        async with self.configuration_lock:
            return await self.configure(request, data)

    async def configure(self, request, data):
        if not os.environ.get("SUPERVISOR_TOKEN") or not self.config.get("supervisor_ingress"):
            raise web.HTTPForbidden(text="Use configure-ha locally outside HA OS")
        action = data.get("action")
        try:
            if action == "preview":
                registry = await self.registries()
                entity_ids = {item["entity_id"]: item["id"] for item in registry["entities"] if item.get("id")}
                files = generate("/homeassistant", data.getall("entity", []),
                                 self.config.get("locale", "en-US"),
                                 self.config.get("custom_skill_id") or None,
                                 self.config.get("event_credentials") or None,
                                 entity_ids=entity_ids)
                self.config_plan = {"files": files, "fingerprints": self.fingerprints(files),
                                    "id": secrets.token_urlsafe(24), "applied": False}
                atomic_write(self.plan_path, json.dumps(self.config_plan))
                content = "<h1>Review configuration</h1>"
                content += ("<p>Selected entities on the same physical HA device become one Alexa endpoint. "
                            "This installs the local Alexa Device Endpoints integration. Restart HA after validation.</p>"
                            "<p>Any previous tool-owned alexa_devices YAML include is migrated. Its old custom "
                            "component directory is moved out of custom_components into a timestamped "
                            "alexa_devices.alexa-backup directory, avoiding Home Assistant's built-in integration.</p>"
                            "<p>When migrating from separate Alexa entities, remove the old devices in Alexa and "
                            "relink the Smart Home skill once. Rooms remain managed in Alexa.</p>")
                for name, value in files.items():
                    if name.startswith("custom_components/"):
                        content += f"<details><summary>{html.escape(name)}</summary><pre>{html.escape(value)}</pre></details>"
                    else:
                        content += f"<h2>{html.escape(name)}</h2><pre>{html.escape(self.redact(value))}</pre>"
                content += self.form("apply-config", "Back up and apply configuration", self.config_plan["id"])
                return self.page(content)
            if action == "apply-config":
                if not self.config_plan:
                    raise ValueError("No saved preview. Return to the connector home page and preview configuration.")
                if data.get("plan") != self.config_plan["id"]:
                    raise ValueError("This preview was replaced. Return to the connector home page and preview again.")
                files = self.config_plan["files"]
                if self.fingerprints(files) != self.config_plan["fingerprints"]:
                    raise ValueError("Configuration changed since preview; preview it again")
                if not self.config_plan["applied"]:
                    stamp = apply("/homeassistant", files)
                    self.config_plan.update(applied=True, fingerprints=self.fingerprints(files), backup=stamp)
                    atomic_write(self.plan_path, json.dumps(self.config_plan))
                try:
                    await self.supervisor("/core/check")
                except (RuntimeError, aiohttp.ClientError, TimeoutError) as error:
                    detail = self.redact(str(error)) or type(error).__name__
                    LOGGER.error("HA configuration check failed: %s", detail)
                    return self.page(
                        "<h1>HA check failed; restart blocked</h1><pre>" + html.escape(detail) +
                        "</pre><p>Backups use suffix .alexa-backup-" +
                        html.escape(self.config_plan.get("backup", "")) + "</p>" +
                        self.form("apply-config", "Retry configuration check", self.config_plan["id"]),
                        status=409,
                    )
                return self.page("<h1>Configuration valid</h1><p>Your configuration passed validation. "
                                 "Restart Home Assistant to load it before linking Alexa.</p>" +
                                 self.form("restart-ha", "Restart Home Assistant now"))
            if action == "restart-ha":
                # Always check again, even if files were changed after the preview.
                await self.supervisor("/core/check")
                await self.supervisor("/core/restart")
                self.plan_path.unlink(missing_ok=True)
                self.config_plan = None
                return self.page("<h1>Restart requested</h1><p>Wait for Home Assistant to finish starting, "
                                 "then return to Alexa and link the skills.</p>")
        except (ValueError, OSError, RuntimeError, aiohttp.ClientError, TimeoutError) as error:
            detail = self.redact(str(error)) or type(error).__name__
            LOGGER.error("Local HA configuration operation failed: %s", detail)
            return self.page("<h1>Configuration needs attention</h1><pre>" + html.escape(detail) + "</pre>",
                             status=409)
        raise web.HTTPBadRequest()

    def form(self, action, label, plan=None):
        return (f"<form method='post'><input type='hidden' name='csrf' value='{self.csrf}'>"
                + (f"<input type='hidden' name='plan' value='{plan}'>" if plan else "") +
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
                return self.page("<h1>Request submitted</h1><p>Return to Alexa to finish linking.</p>")
            raise web.HTTPBadRequest()
        configuration = ""
        if self.config_plan and self.config_plan.get("applied"):
            configuration = self.form("apply-config", "Check applied configuration", self.config_plan["id"])
        states, registry, selected, warnings = [], {}, [], []
        endpoint_status = None
        if self.config.get("supervisor_ingress"):
            try:
                states = await self.ha("GET", "/api/states")
            except (aiohttp.ClientError, RuntimeError, TimeoutError, ValueError) as error:
                warnings.append("Unable to load HA entities: " + (self.redact(str(error)) or type(error).__name__))
            else:
                try:
                    registry = await self.registries()
                except (aiohttp.ClientError, RuntimeError, TimeoutError, ValueError) as error:
                    warnings.append("Room and device information unavailable; entities are shown unassigned. " +
                                    (self.redact(str(error)) or type(error).__name__))
                try:
                    selected = selected_entities("/homeassistant", registry.get("entities", []))
                except (ValueError, OSError) as error:
                    warnings.append(self.redact(str(error)))
                try:
                    endpoint_status = await self.ha("GET", "/api/alexa_device_endpoints")
                    if not isinstance(endpoint_status, dict) or not isinstance(endpoint_status.get("devices"), list):
                        raise ValueError("Invalid native endpoint status response")
                    registry["supported_domains"] = endpoint_status["supported_domains"]
                except HAError as error:
                    if error.status != 404:
                        warnings.append("Could not load native endpoint status: " + self.redact(str(error)))
                    else:
                        warnings.append("Native endpoints are not loaded yet. Preview/apply configuration and restart HA.")
                    endpoint_status = None
                except (aiohttp.ClientError, TimeoutError, RuntimeError, ValueError, KeyError) as error:
                    warnings.append("Could not load native endpoint status: " + self.redact(str(error)))
                    endpoint_status = None
        else:
            warnings.append("For Docker/standalone installations, manage entity selection with configure-ha locally.")
        if warnings:
            LOGGER.warning("Dashboard: %s", " ".join(warnings))
        return web.Response(
            text=dashboard(states, registry, selected, self.csrf, self.ready, self.links, self.grants,
                           configuration, " ".join(warnings), endpoint_status),
            content_type="text/html", headers=self.page_headers(),
        )

    async def ui_state(self, request):
        if not self.trusted(request):
            raise web.HTTPForbidden()
        return web.json_response({"connected": self.ready, "links": self.links, "grants": self.grants},
                                 headers={"Cache-Control": "no-store"})

    async def static(self, request):
        if not self.trusted(request):
            raise web.HTTPForbidden()
        name = request.match_info["name"]
        if name not in ("ui.css", "ui.js"):
            raise web.HTTPNotFound()
        return web.FileResponse(Path(__file__).parent / "static" / name,
                                headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache"})

    async def health(self, request):
        return web.json_response({"alive": True, "connected": self.ready,
                                  "poll_age_seconds": int(time.monotonic() - self.last_poll)
                                  if self.last_poll else None})

    async def start(self):
        self.session = aiohttp.ClientSession()
        app = web.Application(client_max_size=131072)
        app.router.add_route("*", "/", self.ui)
        app.router.add_route("*", "/phones", self.phones_ui)
        app.router.add_get("/ui-state", self.ui_state)
        app.router.add_get("/static/{name}", self.static)
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
