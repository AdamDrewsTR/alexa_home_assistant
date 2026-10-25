import tempfile
import unittest
from pathlib import Path

from addon.ha_config import apply, generate, selected_entities


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.root = self.directory / "configuration.yaml"
        self.original = "# my configuration\nhomeassistant:\n  name: 'My Home'\nlogger: !include logger.yaml\n"
        self.root.write_text(self.original)

    def test_roundtrip_preserves_unrelated_configuration_and_is_repeatable(self):
        files = generate(self.directory, ["light.kitchen"], custom_skill="skill-id")
        self.assertIn("# my configuration", files["configuration.yaml"])
        self.assertIn("name: 'My Home'", files["configuration.yaml"])
        self.assertIn("logger: !include logger.yaml", files["configuration.yaml"])
        stamp = apply(self.directory, files)
        self.assertEqual((self.directory / f"configuration.yaml.alexa-backup-{stamp}").read_text(), self.original)
        repeated = generate(self.directory, ["light.kitchen"], custom_skill="skill-id")
        self.assertEqual(files, repeated)

    def test_existing_alexa_or_intents_are_never_overwritten(self):
        for existing in ("alexa:\n  smart_home:\n", "intent_script: !include existing.yaml\n"):
            self.root.write_text(existing)
            with self.assertRaisesRegex(ValueError, "manual integration"):
                generate(self.directory, ["light.kitchen"], custom_skill="skill-id")
            self.assertEqual(self.root.read_text(), existing)

    def test_empty_or_invalid_allowlist_and_unowned_files_are_rejected(self):
        for entities in ([], ["not_an_entity"], ["light.kitchen/../bad"]):
            with self.assertRaises(ValueError):
                generate(self.directory, entities)
        (self.directory / "alexa_bridge.yaml").write_text("existing: true\n")
        with self.assertRaisesRegex(ValueError, "not owned"):
            generate(self.directory, ["light.kitchen"])

    def test_device_component_install_is_owned_backed_up_and_repeatable(self):
        files = generate(self.directory, ["light.kitchen", "lock.front"],
                         entity_ids={"light.kitchen": "stable-light", "lock.front": "stable-lock"})
        self.assertIn("alexa_devices: !include alexa_devices.yaml", files["configuration.yaml"])
        self.assertNotIn("smart_home:", files["alexa_bridge.yaml"])
        self.assertIn("stable-light", files["alexa_devices.yaml"])
        self.assertIn("custom_components/alexa_devices/manifest.json", files)
        apply(self.directory, files)
        self.assertEqual(selected_entities(self.directory), ["light.kitchen", "lock.front"])
        self.assertEqual(selected_entities(self.directory, [
            {"id": "stable-light", "entity_id": "light.renamed"},
        ]), ["light.renamed", "lock.front"])
        repeated = generate(self.directory, ["light.kitchen", "lock.front"],
                            entity_ids={"light.kitchen": "stable-light", "lock.front": "stable-lock"})
        self.assertEqual(files, repeated)
        stamp = apply(self.directory, files)
        self.assertTrue((self.directory /
                         f"custom_components/alexa_devices/__init__.py.alexa-backup-{stamp}").exists())

    def test_unowned_component_path_traversal_and_symlinks_are_rejected(self):
        component = self.directory / "custom_components/alexa_devices"
        component.mkdir(parents=True)
        (component / "__init__.py").write_text("# Someone else's integration\n")
        with self.assertRaisesRegex(ValueError, "not owned"):
            generate(self.directory, ["light.kitchen"])
        with self.assertRaisesRegex(ValueError, "Only configuration"):
            apply(self.directory, {"../outside.yaml": "unsafe"})
        with tempfile.TemporaryDirectory() as target:
            symlink = self.directory / "linked.yaml"
            symlink.symlink_to(Path(target) / "configuration.yaml")
            with self.assertRaisesRegex(ValueError, "symlinks"):
                apply(self.directory, {"linked.yaml": "unsafe"})
            (component / "bundles.py").symlink_to(Path(target) / "bundles.py")
            with self.assertRaisesRegex(ValueError, "symlinks"):
                apply(self.directory, {"custom_components/alexa_devices/bundles.py": "unsafe"})
