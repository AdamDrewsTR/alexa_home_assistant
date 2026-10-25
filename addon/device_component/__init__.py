# Managed by ha-alexa setup; edit entity selection through setup.
"""Physical-device Alexa endpoints backed by Home Assistant's native adapters."""

import asyncio
from copy import deepcopy
from hashlib import sha256
import json
import logging
import re
from uuid import uuid4

from aiohttp import ClientError

from homeassistant.components.alexa import SMART_HOME_SCHEMA
from homeassistant.components.alexa.auth import Auth
from homeassistant.components.alexa.config import AbstractConfig
from homeassistant.components.alexa.entities import ENTITY_ADAPTERS
from homeassistant.components.alexa.errors import AlexaError, NoTokenAvailable, RequireRelink
from homeassistant.components.alexa.handlers import HANDLERS
from homeassistant.components.alexa.state_report import (
    AlexaDirective, AlexaResponse, async_send_changereport_message,
    async_send_doorbell_event_message, valid_doorbell_timestamp,
)
from homeassistant.components.http import HomeAssistantView
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, EVENT_HOMEASSISTANT_STOP, EVENT_STATE_CHANGED
from homeassistant.core import Context, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .bundles import Bundle, stable_id

DOMAIN = "alexa_device_endpoints"
LOGGER = logging.getLogger(__name__)


def CONFIG_SCHEMA(config):
    if DOMAIN in config:
        value = config[DOMAIN]
        if (not isinstance(value, dict) or not isinstance(value.get("filter"), dict)
                or not value["filter"].get("include_entities")):
            raise ValueError("alexa_device_endpoints requires an explicit nonempty include_entities filter")
        # Use HA's own schema, including its locale, credential, and event-gateway validation.
        SMART_HOME_SCHEMA({key: item for key, item in value.items() if key != "entity_ids"})
        identities = value.get("entity_ids", {})
        if not isinstance(identities, dict) or not all(
            isinstance(key, str) and isinstance(item, str) for key, item in identities.items()
        ):
            raise ValueError("alexa_device_endpoints entity_ids must map entity IDs to registry IDs")
    return config


class DeviceConfig(AbstractConfig):
    def __init__(self, hass, raw):
        super().__init__(hass)
        self.raw = raw
        self.smart = SMART_HOME_SCHEMA({key: item for key, item in raw.items() if key != "entity_ids"})
        self.selected = set(raw["filter"]["include_entities"])
        self.identities = raw.get("entity_ids", {})
        self.storage = Store(hass, 1, DOMAIN)
        self.settings = {}
        self.auth = None
        if self.smart.get("client_id") and self.smart.get("client_secret"):
            self.auth = Auth(hass, self.smart["client_id"], self.smart["client_secret"])
            self.auth._store = Store(hass, 1, DOMAIN + "_auth")
        self.pending = set()
        self.report_task = None
        self.previous = {}
        self.doorbells = []
        self.warning_signature = None
        self.lock = asyncio.Lock()

    async def async_initialize(self):
        self.settings = await self.storage.async_load() or {
            "namespace": uuid4().hex, "authorized": False, "published": {},
        }
        await self.storage.async_save(self.settings)

    @property
    def endpoint(self):
        return self.smart.get("endpoint")

    @property
    def locale(self):
        return self.smart["locale"]

    @property
    def entity_config(self):
        return self.smart.get("entity_config", {})

    @property
    def supports_auth(self):
        return self.auth is not None

    @property
    def authorized(self):
        return self.settings.get("authorized", False)

    @property
    def should_report_state(self):
        return self.supports_auth and self.authorized and bool(self.endpoint)

    def user_identifier(self):
        return self.settings["namespace"]

    async def set_authorized(self, authorized):
        self.settings["authorized"] = authorized
        await self.storage.async_save(self.settings)

    def async_invalidate_access_token(self):
        self.auth.async_invalidate_access_token()

    async def async_get_access_token(self):
        return await self.auth.async_get_access_token() if self.auth else None

    async def async_accept_grant(self, code):
        if not self.auth:
            raise ValueError("Configure proactive-event credentials before linking Alexa")
        token = await self.auth.async_do_auth(code)
        if not token:
            raise ValueError("Amazon rejected the proactive-event grant; check skill credentials")
        await self.set_authorized(True)
        return token

    def resolved_selection(self):
        registry = er.async_get(self.hass)
        by_id = {entry.id: entry.entity_id for entry in registry.entities.values()}
        return {by_id.get(self.identities.get(entity_id), entity_id) for entity_id in self.selected}

    @callback
    def should_expose(self, entity_id):
        return entity_id in self.resolved_selection()

    def bundles(self):
        registry, devices = er.async_get(self.hass), dr.async_get(self.hass)
        grouped, warnings = {}, []
        for entity_id in sorted(self.resolved_selection()):
            entry = registry.async_get(entity_id)
            state = self.hass.states.get(entity_id)
            expected_identity = self.identities.get(entity_id)
            if expected_identity and (entry is None or entry.id != expected_identity):
                warnings.append(f"{entity_id}: selected registry identity was removed; select its replacement explicitly")
                continue
            if not state or (entry and entry.disabled_by) or state.domain not in ENTITY_ADAPTERS:
                warnings.append(f"{entity_id}: unavailable, disabled, or unsupported by HA's Alexa adapter")
                continue
            adapter = ENTITY_ADAPTERS[state.domain](self.hass, self, state)
            try:
                discovery = adapter.serialize_discovery()
            except (ValueError, TypeError, KeyError):
                LOGGER.exception("Could not build Alexa capabilities for %s", entity_id)
                warnings.append(f"{entity_id}: HA could not serialize its Alexa capabilities")
                continue
            caps = discovery["capabilities"]
            if not any(cap["interface"] not in ("Alexa", "Alexa.EndpointHealth") for cap in caps):
                warnings.append(f"{entity_id}: no Alexa capabilities supported in this locale")
                continue
            device = devices.async_get(entry.device_id) if entry and entry.device_id else None
            identity = entry.id if entry else entity_id
            # Scenes and cameras have special responses/lifecycles and remain separate endpoints.
            independent = state.domain in ("scene", "script", "camera")
            group_id = "device:" + device.id if device and not independent else "entity:" + identity
            group = grouped.setdefault(group_id, {
                "name": (device.name_by_user or device.name) if device and not independent else adapter.friendly_name(),
                "manufacturer": device.manufacturer if device else "",
                "model": device.model if device else "", "members": [],
            })
            group["members"].append({"entity_id": entity_id, "identity": identity, "adapter": adapter,
                                     "name": adapter.friendly_name(), "discovery": discovery})
        bundles = {}
        for identity, group in grouped.items():
            name = re.sub(r"[^\w\s]", " ", group["name"] or "Home device", flags=re.UNICODE)
            name = " ".join(name.split())[:256] or "Home device"
            endpoint_id = stable_id(self.user_identifier(), identity)
            bundle = Bundle(endpoint_id, name, group["members"], self.locale,
                            group["manufacturer"], group["model"])
            bundles[endpoint_id] = bundle
            warnings.extend(bundle.warnings)
        if len(bundles) > 300:
            raise ValueError("Alexa supports at most 300 device endpoints")
        signature = tuple(warnings)
        if signature != self.warning_signature:
            for warning in warnings:
                LOGGER.warning("%s", warning)
            self.warning_signature = signature
        return bundles, warnings

    async def publish(self, bundles):
        if not self.should_report_state:
            return
        try:
            token = await self.async_get_access_token()
        except (NoTokenAvailable, RequireRelink):
            await self.set_authorized(False)
            LOGGER.error("Alexa device updates require account relinking")
            return
        if not token:
            LOGGER.error("Alexa device updates require account relinking")
            return
        published = self.settings.get("published", {})
        current = {key: sha256(json.dumps(bundle.discovery, sort_keys=True).encode()).hexdigest()
                   for key, bundle in bundles.items()}
        reports = []
        removed = sorted(set(published) - set(current))
        if removed:
            reports.append(("DeleteReport", [{"endpointId": key} for key in removed]))
        changed = [bundle.serialize_discovery() for key, bundle in bundles.items()
                   if published.get(key) != current[key]]
        # Keep AddOrUpdateReport well below Amazon's 256KB limit.
        reports.extend(("AddOrUpdateReport", [endpoint]) for endpoint in changed)
        session = async_get_clientsession(self.hass)
        for name, endpoints in reports:
            for attempt in range(2):
                message = AlexaResponse(name=name, namespace="Alexa.Discovery", payload={
                    "endpoints": endpoints, "scope": {"type": "BearerToken", "token": token},
                }).serialize()
                async with asyncio.timeout(10):
                    async with session.post(self.endpoint, json=message,
                                            headers={"Authorization": "Bearer " + token},
                                            allow_redirects=False) as response:
                        raw = await response.content.read(8192)
                        status = response.status
                if status in (200, 202, 204):
                    break
                try:
                    error = json.loads(raw)
                except ValueError:
                    error = {}
                invalid_token = status == 401 or (
                    isinstance(error, dict) and isinstance(error.get("payload"), dict)
                    and error["payload"].get("code") == "INVALID_ACCESS_TOKEN_EXCEPTION")
                if invalid_token and not attempt:
                    self.async_invalidate_access_token()
                    try:
                        token = await self.async_get_access_token()
                    except (NoTokenAvailable, RequireRelink):
                        token = None
                    if token:
                        continue
                if invalid_token:
                    await self.set_authorized(False)
                LOGGER.error("Alexa %s failed with HTTP %s; device metadata will retry on next update",
                             name, status)
                return
        self.settings["published"] = current
        await self.storage.async_save(self.settings)

    @callback
    def changed(self, event):
        if not self.hass.is_running and event.event_type != EVENT_HOMEASSISTANT_STARTED:
            return
        if event.event_type == EVENT_STATE_CHANGED and event.data["entity_id"] not in self.resolved_selection():
            return
        if event.event_type == EVENT_STATE_CHANGED:
            entity_id = event.data["entity_id"]
            self.pending.add(entity_id)
            new, old = event.data.get("new_state"), event.data.get("old_state")
            if new and (
                (new.domain == "event" and valid_doorbell_timestamp(entity_id, new.state)
                 and (old is None or old.state not in ("unavailable", new.state)))
                or (new.state == "on" and (old is None or old.state not in ("on", "unavailable")))
            ):
                self.doorbells.append(entity_id)
        else:
            self.pending.update(self.resolved_selection())
        if self.report_task is None or self.report_task.done():
            self.report_task = self.hass.async_create_task(self.report_changes())

    async def report_changes(self):
        while self.pending or self.doorbells:
            await asyncio.sleep(0.5)
            changed, self.pending = self.pending, set()
            doorbells, self.doorbells = self.doorbells, []
            try:
                bundles, _ = self.bundles()
                await self.publish(bundles)
                if not self.should_report_state:
                    continue
                for key, bundle in bundles.items():
                    for entity_id in doorbells:
                        if any(source[0] == entity_id and source[1] == "Alexa.DoorbellEventSource"
                               for source in bundle.transforms):
                            await async_send_doorbell_event_message(self.hass, self, bundle)
                    if not any(member["entity_id"] in changed for member in bundle.members):
                        continue
                    properties = bundle.serialize_properties()
                    values = [(prop["namespace"], prop.get("instance"), prop["name"], prop["value"])
                              for prop in properties]
                    if values == self.previous.get(key):
                        continue
                    await async_send_changereport_message(self.hass, self, bundle, properties)
                    self.previous[key] = values
            except (TimeoutError, OSError, ValueError, KeyError, TypeError, ClientError, HomeAssistantError) as error:
                LOGGER.error("Could not report bundled Alexa device changes (%s)", type(error).__name__)


class DeviceView(HomeAssistantView):
    url = "/api/alexa/smart_home"
    name = "api:alexa_device_endpoints"

    def __init__(self, config):
        self.config = config

    async def post(self, request):
        message = await request.json()
        context = Context(user_id=request["hass_user"].id)
        async with self.config.lock:
            response = await self.handle(message, context)
        return self.json(response)

    async def handle(self, message, context):
        if (not isinstance(message, dict) or not isinstance(message.get("directive"), dict)
                or not isinstance(message["directive"].get("header"), dict)
                or not isinstance(message["directive"].get("payload"), dict)
                or not all(isinstance(message["directive"]["header"].get(key), str)
                           for key in ("namespace", "name"))
                or ("instance" in message["directive"]["header"]
                    and not isinstance(message["directive"]["header"]["instance"], str))
                or ("endpoint" in message["directive"] and (
                    not isinstance(message["directive"]["endpoint"], dict)
                    or not isinstance(message["directive"]["endpoint"].get("endpointId"), str)))):
            return AlexaResponse(name="ErrorResponse", payload={
                "type": "INVALID_DIRECTIVE", "message": "Malformed Alexa directive",
            }).serialize()
        directive = AlexaDirective(message)
        header = message["directive"]["header"]
        if header.get("payloadVersion") != "3":
            return directive.error(error_type="INVALID_DIRECTIVE", error_message="Expected Alexa v3").serialize()
        try:
            if (directive.namespace, directive.name) == ("Alexa.Authorization", "AcceptGrant"):
                await self.config.async_accept_grant(directive.payload["grant"]["code"])
                self.config.pending.update(self.config.resolved_selection())
                if self.config.report_task is None or self.config.report_task.done():
                    self.config.report_task = self.config.hass.async_create_task(self.config.report_changes())
                return directive.response(name="AcceptGrant.Response", namespace="Alexa.Authorization").serialize()
            bundles, _ = self.config.bundles()
            if (directive.namespace, directive.name) == ("Alexa.Discovery", "Discover"):
                result = directive.response(name="Discover.Response", namespace="Alexa.Discovery", payload={
                    "endpoints": [bundle.serialize_discovery() for bundle in bundles.values()],
                }).serialize()
                return result
            endpoint_id = message["directive"].get("endpoint", {}).get("endpointId")
            bundle = bundles.get(endpoint_id)
            if bundle is None:
                return directive.error(error_type="NO_SUCH_ENDPOINT", error_message="Device is no longer exposed").serialize()
            if (directive.namespace, directive.name) == ("Alexa", "ReportState"):
                result = directive.response(name="StateReport").serialize()
                result["context"] = {"properties": bundle.serialize_properties()}
                return result
            route = bundle.route(directive.namespace, header.get("instance"))
            if route is None:
                return directive.error(error_type="INVALID_DIRECTIVE",
                                       error_message="Device does not support this capability").serialize()
            member, namespace, instance = route
            translated = deepcopy(message)
            translated_header = translated["directive"]["header"]
            translated_header["namespace"] = namespace
            translated_header.pop("instance", None)
            if instance is not None:
                translated_header["instance"] = instance
            native = AlexaDirective(translated)
            native.entity_id = member["entity_id"]
            native.entity = self.config.hass.states.get(native.entity_id)
            native.endpoint = member["adapter"]
            native.instance = instance
            handler = HANDLERS.get((namespace, native.name))
            if not handler:
                return directive.error(error_type="INVALID_DIRECTIVE",
                                       error_message="Unsupported Alexa command").serialize()
            response = (await handler(self.config.hass, self.config, native, context)).serialize()
            if response["event"]["header"]["name"] != "ErrorResponse":
                response["context"] = {"properties": bundle.response_properties(member, response)}
            # The native handler retains the original bundled endpoint and correlation token.
            return response
        except AlexaError as error:
            return directive.error(error_type=str(error.error_type), error_message=error.error_message).serialize()
        except (ValueError, KeyError, TypeError, HomeAssistantError, ClientError, TimeoutError) as error:
            LOGGER.error("Bundled Alexa request failed: %s/%s (%s)",
                         directive.namespace, directive.name, type(error).__name__)
            return directive.error(error_message="Unable to process device request").serialize()


class DeviceStatusView(HomeAssistantView):
    url = "/api/alexa_device_endpoints"
    name = "api:alexa_device_endpoints:status"

    def __init__(self, config):
        self.config = config

    async def get(self, request):
        bundles, warnings = self.config.bundles()
        return self.json({"devices": [
            {"name": bundle.discovery["friendlyName"], "endpoint_id": key,
             "entities": [member["entity_id"] for member in bundle.members],
             "capabilities": [cap["interface"] for cap in bundle.capabilities]}
            for key, bundle in bundles.items()], "warnings": warnings,
                          "supported_domains": sorted(ENTITY_ADAPTERS)})


async def async_setup(hass, config):
    if "smart_home" in (config.get("alexa") or {}):
        LOGGER.error("Remove built-in alexa.smart_home before enabling alexa_device_endpoints; both own the same API")
        return False
    settings = DeviceConfig(hass, config[DOMAIN])
    await settings.async_initialize()
    hass.data[DOMAIN] = settings
    hass.http.register_view(DeviceView(settings))
    hass.http.register_view(DeviceStatusView(settings))
    for event_type in (EVENT_STATE_CHANGED, "entity_registry_updated", "device_registry_updated"):
        hass.bus.async_listen(event_type, settings.changed)
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, settings.changed)

    async def stop(event):
        if settings.report_task and not settings.report_task.done():
            settings.report_task.cancel()
            await asyncio.gather(settings.report_task, return_exceptions=True)

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop)
    return True
