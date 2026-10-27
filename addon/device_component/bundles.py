# Managed by ha-alexa setup; edit entity selection through setup.
"""Compose native Alexa capabilities without changing Home Assistant's adapters."""

from copy import deepcopy
from hashlib import sha256


GENERIC = {
    "Alexa.RangeController", "Alexa.ModeController", "Alexa.ToggleController",
    "Alexa.InventoryLevelSensor",
}
COMMON = {"Alexa", "Alexa.EndpointHealth"}
PRIORITY = {"alarm_control_panel": 0, "lock": 1, "climate": 2, "light": 3,
            "fan": 4, "cover": 5, "switch": 6}


def stable_id(namespace, identity):
    return "ha-device-" + sha256((namespace + ":" + identity).encode()).hexdigest()[:32]


class Bundle:
    """One endpoint, with explicit bidirectional capability ownership."""

    def __init__(self, endpoint_id, name, members, locale="en-US", manufacturer="", model=""):
        self.endpoint_id = endpoint_id
        self.entity_id = members[0]["entity_id"]
        self.members = sorted(members, key=lambda member: (
            PRIORITY.get(member["entity_id"].split(".")[0], 10), member["identity"]))
        self.routes = {}
        self.transforms = {}
        self.warnings = []
        self.capabilities = []
        seen = set()
        for member in self.members:
            for original in member["discovery"]["capabilities"]:
                cap = deepcopy(original)
                interface = cap["interface"]
                old_instance = cap.get("instance")
                source_key = (member["entity_id"], interface, old_instance)
                if interface in COMMON:
                    if interface not in seen:
                        self.capabilities.append(cap)
                        seen.add(interface)
                    continue
                target_interface = interface
                if interface == "Alexa.PowerController" and interface in seen:
                    target_interface = "Alexa.ToggleController"
                    cap["interface"] = target_interface
                    cap["properties"]["supported"] = [{"name": "toggleState"}]
                    cap["capabilityResources"] = {"friendlyNames": [{
                        "@type": "text", "value": {"text": member["name"][:128], "locale": locale},
                    }]}
                if target_interface in GENERIC:
                    instance = "component." + sha256(
                        (member["identity"] + ":" + interface + ":" + str(old_instance)).encode()
                    ).hexdigest()[:16]
                    cap["instance"] = instance
                else:
                    instance = old_instance
                    if interface in seen:
                        self.warnings.append(
                            f"{member['entity_id']}: {interface} already supplied by another entity; "
                            "Alexa allows only one instance on a device")
                        continue
                self.capabilities.append(cap)
                seen.add(interface)
                self.routes[(target_interface, instance)] = (member, interface, old_instance)
                self.transforms[source_key] = (target_interface, instance)
        if len(self.capabilities) > 100:
            raise ValueError("Device exceeds Alexa's 100-capability limit; reduce its selection")
        primary = self.members[0]["discovery"]
        self.discovery = {
            "endpointId": endpoint_id, "friendlyName": name,
            "description": "Device connected through Home Assistant",
            "manufacturerName": manufacturer or "Home Assistant",
            "displayCategories": primary.get("displayCategories") or ["OTHER"],
            "additionalAttributes": {"manufacturer": manufacturer or "Home Assistant",
                                     "model": model or "Bundled device", "customIdentifier": endpoint_id},
            "cookie": {}, "capabilities": self.capabilities,
        }
        for member in self.members:
            if not any(key[0] == member["entity_id"] for key in self.transforms):
                self.warnings.append(f"{member['entity_id']}: no distinct supported capability on this device")

    def alexa_id(self):
        return self.endpoint_id

    def serialize_discovery(self):
        return deepcopy(self.discovery)

    def translate_properties(self, entity_id, properties):
        result = []
        for prop in properties:
            if prop["namespace"] == "Alexa.EndpointHealth":
                continue
            target = self.transforms.get((entity_id, prop["namespace"], prop.get("instance")))
            if target is None:
                continue
            translated = deepcopy(prop)
            translated["namespace"] = target[0]
            if target[1] is None:
                translated.pop("instance", None)
            else:
                translated["instance"] = target[1]
            if prop["namespace"] == "Alexa.PowerController" and target[0] == "Alexa.ToggleController":
                translated["name"] = "toggleState"
            result.append(translated)
        return result

    def serialize_properties(self):
        properties, health = [], []
        for member in self.members:
            values = list(member["adapter"].serialize_properties())
            for capability in member.get("supplemental", ()):
                values.extend(capability.serialize_properties())
            properties.extend(self.translate_properties(member["entity_id"], values))
            health.extend(prop for prop in values if prop["namespace"] == "Alexa.EndpointHealth")
        if health:
            aggregate = deepcopy(health[0])
            if any(prop["value"].get("value") != "OK" for prop in health):
                aggregate["value"] = {"value": "UNREACHABLE"}
            aggregate["uncertaintyInMilliseconds"] = max(
                prop.get("uncertaintyInMilliseconds", 0) for prop in health)
            properties.append(aggregate)
        return properties

    def route(self, namespace, instance):
        return self.routes.get((namespace, instance))

    def response_properties(self, member, response):
        properties = self.translate_properties(
            member["entity_id"], response.get("context", {}).get("properties", []))
        keys = {(prop["namespace"], prop.get("instance"), prop["name"]) for prop in properties}
        properties.extend(prop for prop in self.serialize_properties()
                          if (prop["namespace"], prop.get("instance"), prop["name"]) not in keys)
        return properties
