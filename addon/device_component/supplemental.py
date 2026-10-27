"""Alexa-native adapters and sensor interfaces not yet provided by HA's registry."""

from collections.abc import Generator
from hashlib import sha256
from math import isfinite

from homeassistant.components import lawn_mower
from homeassistant.components.alexa.capabilities import (
    Alexa,
    AlexaCapability,
    AlexaEndpointHealth,
    AlexaPowerController,
)
from homeassistant.components.alexa.entities import AlexaEntity, DisplayCategory
from homeassistant.const import EntityStateAttribute, UnitOfTemperature
from homeassistant.core import HomeAssistant, State


HUMIDITY_LOCALES = {
    "ar-SA", "de-DE", "en-AU", "en-CA", "en-GB", "en-IN", "en-US", "es-ES",
    "es-MX", "es-US", "fr-CA", "fr-FR", "hi-IN", "it-IT", "ja-JP", "pt-BR",
}
INVENTORY_LOCALES = {
    "de-DE", "en-CA", "en-GB", "en-US", "es-ES", "es-MX", "es-US",
    "fr-CA", "fr-FR", "it-IT", "ja-JP",
}


def _numeric(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


def _percentage(value):
    result = _numeric(value)
    return result if result is not None and 0 <= result <= 100 else None


class HumiditySensor(AlexaCapability):
    """Alexa humidity interface for HA's native humidity measurements."""

    supported_locales = HUMIDITY_LOCALES

    def __init__(self, entity, source):
        super().__init__(entity)
        self.source = source

    def name(self):
        return "Alexa.HumiditySensor"

    def properties_supported(self):
        return [{"name": "relativeHumidity"}]

    def properties_proactively_reported(self):
        return True

    def properties_retrievable(self):
        return True

    def get_property(self, name):
        if name != "relativeHumidity":
            return None
        if self.source == "state":
            value = self.entity.state
        else:
            value = self.entity.attributes.get(self.source)
        return _percentage(value)


class TemperatureSensor(AlexaCapability):
    """Alexa temperature interface for HA weather entities."""

    supported_locales = HUMIDITY_LOCALES

    def __init__(self, hass, entity):
        super().__init__(entity)
        self.hass = hass

    def name(self):
        return "Alexa.TemperatureSensor"

    def properties_supported(self):
        return [{"name": "temperature"}]

    def properties_proactively_reported(self):
        return True

    def properties_retrievable(self):
        return True

    def get_property(self, name):
        if name != "temperature":
            return None
        value = _numeric(self.entity.attributes.get("temperature"))
        unit = self.entity.attributes.get("temperature_unit")
        if unit is None:
            unit = self.hass.config.units.temperature_unit
        if value is None:
            return None
        if unit == UnitOfTemperature.CELSIUS:
            scale = "CELSIUS"
        elif unit == UnitOfTemperature.FAHRENHEIT:
            scale = "FAHRENHEIT"
        else:
            return None
        return {"value": value, "scale": scale}


class InventoryLevelSensor(AlexaCapability):
    """Alexa inventory level for replaceable HA battery sensors."""

    supported_locales = INVENTORY_LOCALES

    def __init__(self, entity, instance, locale):
        super().__init__(entity, instance=instance)
        self.locale = locale

    def name(self):
        return "Alexa.InventoryLevelSensor"

    def properties_supported(self):
        return [{"name": "level"}]

    def properties_proactively_reported(self):
        return True

    def properties_retrievable(self):
        return True

    def configuration(self):
        return {"measurement": {"@type": "percentage"}}

    def capability_resources(self):
        name = self.entity.attributes.get("friendly_name") or "Battery level"
        return {"friendlyNames": [{
            "@type": "text",
            "value": {"text": str(name)[:128], "locale": self.locale},
        }]}

    def get_property(self, name):
        if name != "level":
            return None
        value = _percentage(self.entity.state)
        if value is None:
            return None
        return {"@type": "percentage", "value": str(value)}


class SirenPowerController(AlexaPowerController):
    def get_property(self, name):
        if name != "powerState":
            return None
        return "ON" if self.entity.state == "on" else "OFF"


class LawnMowerPowerController(AlexaPowerController):
    def get_property(self, name):
        if name != "powerState":
            return None
        return "ON" if self.entity.state in ("mowing", "returning") else "OFF"


class SirenEntity(AlexaEntity):
    def default_display_categories(self):
        return [DisplayCategory.OTHER]

    def interfaces(self) -> Generator[AlexaCapability]:
        yield SirenPowerController(self.entity)
        yield AlexaEndpointHealth(self.hass, self.entity)
        yield Alexa(self.entity)


class LawnMowerEntityAdapter(AlexaEntity):
    def default_display_categories(self):
        return [DisplayCategory.VACUUM_CLEANER]

    def interfaces(self) -> Generator[AlexaCapability]:
        features = self.entity.attributes.get(EntityStateAttribute.SUPPORTED_FEATURES, 0)
        if (features & lawn_mower.LawnMowerEntityFeature.START_MOWING
                and features & (lawn_mower.LawnMowerEntityFeature.PAUSE
                                | lawn_mower.LawnMowerEntityFeature.STOP)):
            yield LawnMowerPowerController(self.entity)
        yield AlexaEndpointHealth(self.hass, self.entity)
        yield Alexa(self.entity)


class WeatherEntity(AlexaEntity):
    def default_display_categories(self):
        return [DisplayCategory.TEMPERATURE_SENSOR]

    def interfaces(self) -> Generator[AlexaCapability]:
        attrs = self.entity.attributes
        unit = attrs.get("temperature_unit") or self.hass.config.units.temperature_unit
        if _numeric(attrs.get("temperature")) is not None and unit in (
            UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT,
        ):
            yield TemperatureSensor(self.hass, self.entity)
        if _percentage(attrs.get("humidity")) is not None:
            yield HumiditySensor(self.entity, "humidity")
        yield AlexaEndpointHealth(self.hass, self.entity)
        yield Alexa(self.entity)


LOCAL_ADAPTERS = {
    "siren": SirenEntity,
    "lawn_mower": LawnMowerEntityAdapter,
    "weather": WeatherEntity,
}


def supplemental_capabilities(hass: HomeAssistant, config, state: State):
    attrs = state.attributes
    capabilities = []
    if (state.domain == "sensor" and attrs.get(EntityStateAttribute.DEVICE_CLASS) == "humidity"
            and attrs.get(EntityStateAttribute.UNIT_OF_MEASUREMENT) == "%"):
        capabilities.append(HumiditySensor(state, "state"))
    elif state.domain == "climate" and _percentage(attrs.get("current_humidity")) is not None:
        capabilities.append(HumiditySensor(state, "current_humidity"))
    if (state.domain == "sensor" and attrs.get(EntityStateAttribute.DEVICE_CLASS) == "battery"
            and attrs.get(EntityStateAttribute.UNIT_OF_MEASUREMENT) == "%"):
        identity = sha256(state.entity_id.encode()).hexdigest()[:16]
        capabilities.append(InventoryLevelSensor(state, "battery." + identity, config.locale))
    return [capability for capability in capabilities
            if config.locale in capability.supported_locales]
