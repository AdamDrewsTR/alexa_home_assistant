"""Conservative, round-trip YAML provisioning for Home Assistant."""

import io
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import TaggedScalar
from ruamel.yaml.error import YAMLError


MARKER = "# Managed by ha-alexa setup; edit entity selection through setup.\n"
COMPONENT = Path("custom_components/alexa_device_endpoints")
COMPONENT_FILES = ("__init__.py", "bundles.py", "supplemental.py", "manifest.json")


def selected_entities(directory, registry_entities=()):
    directory = Path(directory)
    path = directory / "alexa_device_endpoints.yaml"
    if not path.exists():
        path = directory / "alexa_devices.yaml"
    bundled = path.exists()
    if not bundled:
        path = directory / "alexa_bridge.yaml"
    if not path.exists():
        return []
    text = path.read_text()
    if not text.startswith(MARKER):
        raise ValueError("Existing Alexa configuration is manually managed; selection was not imported")
    try:
        config = YAML(typ="safe").load(text)
        entities = (config if bundled else config["smart_home"])["filter"]["include_entities"]
        if not isinstance(entities, list) or not all(isinstance(entity, str) for entity in entities):
            raise ValueError("Invalid include_entities list")
        by_id = {entry["id"]: entry["entity_id"] for entry in registry_entities if entry.get("id")}
        identities = config.get("entity_ids", {}) if bundled else {}
        if not isinstance(identities, dict):
            raise ValueError("Invalid entity_ids mapping")
        return [by_id.get(identities.get(entity_id), entity_id) for entity_id in entities]
    except (YAMLError, KeyError, TypeError, ValueError) as error:
        raise ValueError(f"Cannot read selected entities from {path.name}; inspect its filter") from error


def atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".alexa-")
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def render(yaml, value):
    output = io.StringIO()
    yaml.dump(value, output)
    return output.getvalue()


def generate(directory, entities, locale="en-US", custom_skill=None, event_credentials=None,
             entity_ids=None):
    """Return proposed file contents without writing; refuse ownership conflicts."""
    directory = Path(directory)
    root = directory / "configuration.yaml"
    original = root.read_text()
    yaml = YAML()
    yaml.preserve_quotes = True
    config = yaml.load(original)
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise ValueError("configuration.yaml must contain a mapping")
    if "alexa_devices" in config:
        old = config["alexa_devices"]
        old_file = directory / "alexa_devices.yaml"
        if not (isinstance(old, TaggedScalar) and str(old.tag) == "!include"
                and str(old) == "alexa_devices.yaml" and old_file.is_file()
                and old_file.read_text().startswith(MARKER)):
            raise ValueError("Existing alexa_devices: configuration needs manual integration; no files changed")
        del config["alexa_devices"]
    desired = {"alexa": "alexa_bridge.yaml", "alexa_device_endpoints": "alexa_device_endpoints.yaml"}
    if custom_skill:
        desired["intent_script"] = "alexa_bridge_intents.yaml"
    for key, filename in desired.items():
        existing = config.get(key)
        if key in config and not (
            isinstance(existing, TaggedScalar)
            and str(existing.tag) == "!include" and str(existing) == filename
        ):
            raise ValueError(f"Existing {key}: configuration needs manual integration; no files changed")
        target = directory / filename
        if target.exists() and not target.read_text().startswith(MARKER):
            raise ValueError(f"{filename} already exists and is not owned by this tool")
        config[key] = TaggedScalar(filename, tag="!include")
    if not entities:
        raise ValueError("Select at least one entity; an empty filter would expose everything")
    if any(not isinstance(entity, str) or entity.count(".") != 1
           or not all(part.replace("_", "").isalnum() for part in entity.split("."))
           for entity in entities):
        raise ValueError("Invalid entity ID")
    smart = {"locale": locale, "filter": {"include_entities": sorted(set(entities))}}
    if event_credentials:
        smart.update(event_credentials)
    if entity_ids:
        smart["entity_ids"] = {key: value for key, value in entity_ids.items() if key in entities}
    # Built-in Alexa remains responsible for Custom intents, not Smart Home.
    files = {"alexa_bridge.yaml": MARKER + "{}\n",
             "alexa_device_endpoints.yaml": MARKER + render(yaml, smart)}
    source = Path(__file__).parent / "device_component"
    target = directory / COMPONENT
    if target.exists():
        marker = target / "__init__.py"
        if not marker.exists() or not marker.read_text().startswith(MARKER):
            raise ValueError("Existing alexa_device_endpoints custom component is not owned by this tool")
    for name in COMPONENT_FILES:
        files[str(COMPONENT / name)] = (source / name).read_text()
    if custom_skill:
        intents = {
            "HomeStatusIntent": {"speech": {"type": "plain", "text": "Home Assistant is connected."}},
            custom_skill: {
                "speech": {"type": "plain", "text": "You can ask for the home status."},
                "reprompt": {"type": "plain", "text": "Would you like the home status?"},
            },
        }
        for name, text in {
            "Help": "You can ask for the home status.",
            "Cancel": "Goodbye.", "Stop": "Goodbye.",
            "Fallback": "Try asking for the home status.",
        }.items():
            intents[f"AMAZON.{name}Intent"] = {"speech": {"type": "plain", "text": text}}
        files["alexa_bridge_intents.yaml"] = MARKER + render(yaml, intents)
    files["configuration.yaml"] = render(yaml, config)
    return files


def apply(directory, files):
    """Back up every existing target before changing any file."""
    directory = Path(directory)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    backups = {}
    old_component = directory / "custom_components/alexa_devices"
    retired = directory / f"alexa_devices.alexa-backup-{stamp}"
    existing_config = YAML().load((directory / "configuration.yaml").read_text()) or {}
    old_include = existing_config.get("alexa_devices") if isinstance(existing_config, dict) else None
    retire = (str(COMPONENT / "__init__.py") in files and old_component.exists()
              and isinstance(old_include, TaggedScalar) and str(old_include.tag) == "!include"
              and str(old_include) == "alexa_devices.yaml")
    if retire:
        marker = old_component / "__init__.py"
        if (old_component.is_symlink() or old_component.parent.is_symlink()
                or marker.is_symlink() or not marker.is_file()
                or not marker.read_text().startswith(MARKER)):
            raise ValueError("Old alexa_devices custom component is not owned by this tool or uses symlinks")
    for name in files:
        if Path(name).name != name and name not in {str(COMPONENT / part) for part in COMPONENT_FILES}:
            raise ValueError("Only configuration files and the owned Alexa component may be changed")
        path = directory / name
        relative = Path(name)
        if path.is_symlink() or any((directory / parent).is_symlink()
                                   for parent in relative.parents if str(parent) != "."):
            raise ValueError("Refusing to write configuration through symlinks")
        if not path.resolve().is_relative_to(directory.resolve()):
            raise ValueError("Configuration target must stay inside the configuration directory")
        if path.exists():
            backups[name] = path.read_text()
            atomic_write(directory / f"{name}.alexa-backup-{stamp}", backups[name])
    # Included files must exist before the root file starts referring to them.
    try:
        for name in sorted(files, key=lambda name: name == "configuration.yaml"):
            atomic_write(directory / name, files[name])
        if retire:
            os.replace(old_component, retired)
    except OSError:
        for name in files:
            if name in backups:
                atomic_write(directory / name, backups[name])
            else:
                (directory / name).unlink(missing_ok=True)
        raise
    return stamp
