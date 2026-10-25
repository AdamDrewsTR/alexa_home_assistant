import tempfile
import unittest
from pathlib import Path

from addon.ha_config import apply, generate


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
