"""Conservative, round-trip YAML provisioning for Home Assistant."""

import io
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import TaggedScalar


MARKER = "# Managed by ha-alexa setup; edit entity selection through setup.\n"


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


def generate(directory, entities, locale="en-US", custom_skill=None, event_credentials=None):
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
    desired = {"alexa": "alexa_bridge.yaml"}
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
    files = {"alexa_bridge.yaml": MARKER + render(yaml, {"smart_home": smart})}
    if event_credentials:
        smart.update(event_credentials)
        files["alexa_bridge.yaml"] = MARKER + render(yaml, {"smart_home": smart})
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
    for name in files:
        if Path(name).name != name:
            raise ValueError("Only direct configuration files may be changed")
        path = directory / name
        if path.exists():
            backups[name] = path.read_text()
            atomic_write(directory / f"{name}.alexa-backup-{stamp}", backups[name])
    # Included files must exist before the root file starts referring to them.
    try:
        for name in sorted(files, key=lambda name: name == "configuration.yaml"):
            atomic_write(directory / name, files[name])
    except OSError:
        for name in files:
            if name in backups:
                atomic_write(directory / name, backups[name])
            else:
                (directory / name).unlink(missing_ok=True)
        raise
    return stamp
