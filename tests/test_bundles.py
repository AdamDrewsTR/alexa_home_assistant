import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock


SPEC = importlib.util.spec_from_file_location(
    "bundles", Path(__file__).resolve().parents[1] / "addon/device_component/bundles.py")
bundles = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bundles)


def capability(interface, names=(), instance=None):
    value = {"type": "AlexaInterface", "interface": interface, "version": "3"}
    if names:
        value["properties"] = {"supported": [{"name": name} for name in names],
                               "proactivelyReported": True, "retrievable": True}
    if instance is not None:
        value["instance"] = instance
    return value


def member(entity_id, interfaces, properties=(), identity=None):
    return {"entity_id": entity_id, "identity": identity or entity_id, "name": entity_id,
            "discovery": {"capabilities": [capability("Alexa"), capability("Alexa.EndpointHealth"),
                                          *interfaces], "displayCategories": ["SMARTPLUG"]},
            "adapter": Mock(serialize_properties=Mock(return_value=list(properties)))}


def prop(namespace, name, value, instance=None):
    value = {"namespace": namespace, "name": name, "value": value,
             "timeOfSample": "2026-10-25T12:00:00Z", "uncertaintyInMilliseconds": 0}
    if instance:
        value["instance"] = instance
    return value


class BundleTests(unittest.TestCase):
    def test_one_device_combines_switch_temperature_and_contact(self):
        members = [
            member("switch.plug", [capability("Alexa.PowerController", ["powerState"])],
                   [prop("Alexa.PowerController", "powerState", "ON")]),
            member("sensor.temperature", [capability("Alexa.TemperatureSensor", ["temperature"])],
                   [prop("Alexa.TemperatureSensor", "temperature", {"value": 20, "scale": "CELSIUS"})]),
            member("binary_sensor.contact", [capability("Alexa.ContactSensor", ["detectionState"])]),
        ]
        bundle = bundles.Bundle("device-id", "Room device", members)
        self.assertEqual(len(bundle.discovery["capabilities"]), 5)
        self.assertEqual(bundle.route("Alexa.PowerController", None)[0]["entity_id"], "switch.plug")
        self.assertEqual({value["namespace"] for value in bundle.serialize_properties()},
                         {"Alexa.PowerController", "Alexa.TemperatureSensor"})

    def test_secondary_power_controls_use_native_named_toggle_instances(self):
        members = [
            member("switch.main", [capability("Alexa.PowerController", ["powerState"])],
                   [prop("Alexa.PowerController", "powerState", "ON")], "a"),
            member("switch.child_lock", [capability("Alexa.PowerController", ["powerState"])],
                   [prop("Alexa.PowerController", "powerState", "OFF")], "b"),
        ]
        bundle = bundles.Bundle("device-id", "Plug", members)
        toggle = next(cap for cap in bundle.capabilities if cap["interface"] == "Alexa.ToggleController")
        self.assertEqual(toggle["properties"]["supported"], [{"name": "toggleState"}])
        route = bundle.route("Alexa.ToggleController", toggle["instance"])
        self.assertEqual(route[0]["entity_id"], "switch.child_lock")
        self.assertEqual(route[1:], ("Alexa.PowerController", None))
        properties = bundle.serialize_properties()
        self.assertEqual(properties[1]["name"], "toggleState")
        self.assertEqual(properties[1]["instance"], toggle["instance"])
        self.assertEqual(bundle.warnings, [])

    def test_multiple_generic_controllers_remap_instances_both_directions(self):
        members = [member(f"fan.{name}", [capability("Alexa.RangeController", ["rangeValue"], "fan.speed")],
                          [prop("Alexa.RangeController", "rangeValue", 25, "fan.speed")])
                   for name in ("one", "two")]
        bundle = bundles.Bundle("device-id", "Dual fan", members)
        ranges = [cap for cap in bundle.capabilities if cap["interface"] == "Alexa.RangeController"]
        self.assertEqual(len({cap["instance"] for cap in ranges}), 2)
        for cap in ranges:
            self.assertEqual(bundle.route("Alexa.RangeController", cap["instance"])[2], "fan.speed")
        self.assertEqual({prop["instance"] for prop in bundle.serialize_properties()},
                         {cap["instance"] for cap in ranges})

    def test_specific_interface_conflicts_are_explicit_not_extra_endpoints(self):
        members = [member(f"sensor.{name}", [capability("Alexa.TemperatureSensor", ["temperature"])])
                   for name in ("one", "two")]
        bundle = bundles.Bundle("device-id", "Sensor", members)
        self.assertEqual(sum(cap["interface"] == "Alexa.TemperatureSensor" for cap in bundle.capabilities), 1)
        self.assertTrue(any("sensor.two" in warning and "only one instance" in warning
                            for warning in bundle.warnings))
        self.assertIsNone(bundle.route("Alexa.LockController", None))

    def test_aggregate_health_and_optimistic_control_response_keep_other_member_properties(self):
        members = [
            member("switch.plug", [capability("Alexa.PowerController", ["powerState"])], [
                prop("Alexa.PowerController", "powerState", "OFF"),
                prop("Alexa.EndpointHealth", "connectivity", {"value": "OK"})]),
            member("sensor.temperature", [capability("Alexa.TemperatureSensor", ["temperature"])], [
                prop("Alexa.TemperatureSensor", "temperature", {"value": 20, "scale": "CELSIUS"}),
                prop("Alexa.EndpointHealth", "connectivity", {"value": "UNREACHABLE"})]),
        ]
        bundle = bundles.Bundle("device-id", "Plug", members)
        properties = bundle.response_properties(members[0], {
            "context": {"properties": [prop("Alexa.PowerController", "powerState", "ON")]}})
        self.assertEqual(len(properties), 3)
        self.assertEqual(properties[0]["value"], "ON")
        self.assertEqual(properties[-1]["value"], {"value": "UNREACHABLE"})

    def test_stable_identity_and_instances_survive_rename_order_and_restarts(self):
        identity = bundles.stable_id("home-namespace", "device-registry-id")
        self.assertEqual(identity, bundles.stable_id("home-namespace", "device-registry-id"))
        self.assertNotEqual(identity, bundles.stable_id("other-home", "device-registry-id"))
        first = member("fan.before", [capability("Alexa.RangeController", ["rangeValue"], "speed")],
                       identity="registry-id")
        second = member("fan.after", [capability("Alexa.RangeController", ["rangeValue"], "speed")],
                        identity="registry-id")
        self.assertEqual(bundles.Bundle(identity, "Before", [first]).capabilities,
                         bundles.Bundle(identity, "After", [second]).capabilities)

    def test_capability_limit_includes_shared_interfaces(self):
        members = [member(f"number.control_{index}", [
            capability("Alexa.RangeController", ["rangeValue"], "number.value")])
            for index in range(99)]
        self.assertEqual(len(bundles.Bundle("device-id", "Large device", members[:98]).capabilities), 100)
        with self.assertRaisesRegex(ValueError, "100-capability"):
            bundles.Bundle("device-id", "Too large", members)
