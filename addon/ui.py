"""Local-only dashboard rendering."""

from collections import defaultdict
from html import escape


DOMAINS = {
    "light", "switch", "script", "scene", "climate", "fan", "cover",
    "sensor", "binary_sensor", "media_player", "vacuum", "lock",
    "alarm_control_panel", "camera", "humidifier", "water_heater", "valve",
    "number", "input_number", "input_boolean", "button", "input_button",
    "remote", "event", "timer", "automation", "group", "alert", "image_processing",
}

ICONS = {
    "home": "M3 10 12 3l9 7v11h-6v-7H9v7H3Z",
    "light": "M9 18h6m-5 3h4M8 14a6 6 0 1 1 8 0l-1 2H9Z",
    "sensor": "M12 12h.01M8 8a6 6 0 0 0 0 8m8-8a6 6 0 0 1 0 8M5 5a10 10 0 0 0 0 14M19 5a10 10 0 0 1 0 14",
    "climate": "M9 14V5a3 3 0 0 1 6 0v9a5 5 0 1 1-6 0Z",
    "switch": "M12 3v9m-6-7a9 9 0 1 0 12 0",
    "media_player": "M3 4h18v13H3Zm4 17h10m-5-4v4M10 8l5 3-5 3Z",
    "cover": "M3 3h18v18H3Zm0 5h18M3 13h18M3 18h18",
    "fan": "M12 12c-8-7 2-14 3-7 1 4-3 7-3 7Zm0 0c10-3 10 9 3 7-4-1-3-7-3-7Zm0 0c-2 10-12 4-7 0 3-3 7 0 7 0Z",
    "scene": "m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5Z",
    "device": "M5 3h14v18H5Zm4 14h6",
    "lock": "M5 10h14v11H5Zm3 0V6a4 4 0 0 1 8 0v4m-4 4v3",
    "alarm_control_panel": "m12 2 8 4v6c0 5-8 10-8 10S4 17 4 12V6Zm0 5v6m0 4h.01",
    "camera": "M3 6h13v12H3Zm13 4 5-3v10l-5-3",
    "search": "M10 18a8 8 0 1 0 0-16 8 8 0 0 0 0 16Zm6-2 6 6",
    "arrow": "M4 12h16m-6-6 6 6-6 6",
    "check": "m5 12 4 4L19 6",
}


def icon(name):
    path = ICONS.get(name, ICONS["device"])
    return (f"<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.6' "
            f"stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'><path d='{path}'/></svg>")


def document(content, csrf=""):
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<meta name='color-scheme' content='dark'><title>Alexa Connector</title>"
            "<link rel='stylesheet' href='static/ui.css'>"
            "<script src='static/ui.js' defer></script></head>"
            f"<body data-csrf='{escape(csrf, quote=True)}'>{content}</body></html>")


def organize(states, registry, selected=()):
    """Keep physical devices in one area so UI cards match Alexa endpoint identity."""
    entities = {item["entity_id"]: item for item in registry.get("entities", [])}
    devices = {item["id"]: item for item in registry.get("devices", [])}
    areas = {item["area_id"]: item for item in registry.get("areas", [])}
    floors = {item["floor_id"]: item for item in registry.get("floors", [])}
    device_areas = {item["id"]: item["area_id"] for item in devices.values() if item.get("area_id")}
    for entity in sorted(entities.values(), key=lambda item: item["entity_id"]):
        if entity.get("device_id") and entity.get("area_id"):
            device_areas.setdefault(entity["device_id"], entity["area_id"])
    selected = set(selected)
    states = list(states)
    present = {state["entity_id"] for state in states}
    states.extend({"entity_id": entity_id, "state": "unavailable", "attributes": {}}
                  for entity_id in sorted(selected - present))
    groups = {}
    for state in states:
        entity_id = state["entity_id"]
        domain = entity_id.split(".")[0]
        entity = entities.get(entity_id, {})
        if (domain not in set(registry.get("supported_domains", DOMAINS)) or entity.get("disabled_by")) and entity_id not in selected:
            continue
        device = devices.get(entity.get("device_id"), {})
        area_id = device_areas.get(device.get("id")) or entity.get("area_id") or ""
        area = areas.get(area_id, {})
        floor = floors.get(area.get("floor_id"), {})
        if area_id not in groups:
            groups[area_id] = {
                "id": area_id, "name": area.get("name", "Unassigned"),
                "floor": floor.get("name", "Other areas"),
                "level": floor.get("level") if floor.get("level") is not None else 999,
                "devices": {},
            }
        device_id = device.get("id") or "standalone:" + domain
        card = groups[area_id]["devices"].setdefault(device_id, {
            "physical": bool(device),
            "name": device.get("name_by_user") or device.get("name") or
                    domain.replace("_", " ").title() + " entities",
            "manufacturer": device.get("manufacturer") or "",
            "model": device.get("model") or "",
            "entities": [],
        })
        attributes = state.get("attributes") or {}
        card["entities"].append({
            "id": entity_id, "domain": domain,
            "name": attributes.get("friendly_name") or entity.get("name") or entity_id,
            "state": state.get("state", "unknown"),
            "unit": str(attributes.get("unit_of_measurement") or ""),
            "category": entity.get("entity_category") or "",
        })
    result = sorted(groups.values(), key=lambda item: (
        not bool(item["id"]), item["level"], item["floor"].casefold(), item["name"].casefold()))
    for area in result:
        area["devices"] = sorted(area["devices"].values(), key=lambda item: item["name"].casefold())
        for device in area["devices"]:
            device["entities"].sort(key=lambda item: (
                item["domain"] in ("sensor", "binary_sensor"), item["name"].casefold()))
    return result


def dashboard(states, registry, selected, csrf, connected, links, grants, configuration, warning="",
              endpoint_status=None):
    areas = organize(states, registry, selected)
    all_entities = [entity for area in areas for device in area["devices"] for entity in device["entities"]]
    selected = set(selected)
    selected_count = sum(entity["id"] in selected for entity in all_entities)
    device_count = sum(len(area["devices"]) for area in areas)
    floor_count = len({area["floor"] for area in areas if area["floor"] != "Other areas"})
    endpoint_by_entity = {
        entity_id: device for device in (endpoint_status or {}).get("devices", [])
        for entity_id in device["entities"]
    }
    esc = lambda value: escape(str(value), quote=True)
    content = [
        "<div class='shell'><header class='topbar'><a class='brand' href=''>",
        "<span class='brand-mark'>" + icon("home") + "</span>",
        "<span>alexa<span class='brand-divider'> / </span><strong>home assistant</strong></span></a>",
        "<div class='topbar-right'><span class='private-badge'>Outbound only</span>",
        f"<span class='connection {'online' if connected else 'offline'}' id='connection-status'>",
        "<span class='status-dot'></span><span id='connection-label'>",
        "Relay connected" if connected else "Relay disconnected",
        "</span></span></div></header>",
        "<section class='hero'><div><div class='eyebrow'>YOUR HOME. YOUR CONTROL.</div>",
        "<h1>A home that<br><span>speaks your language.</span></h1>",
        "<p>Bring the right parts of your home to Alexa.<br>"
        "Organized just like Home Assistant. Connected on your terms.</p></div>",
        "<div class='home-signal' aria-hidden='true'><div class='signal-ring ring-one'></div>"
        "<div class='signal-ring ring-two'></div><div class='signal-core'>" + icon("home") +
        "</div><span class='signal-label'>LOCAL HOME &nbsp; / &nbsp; PRIVATE LINK</span></div></section>",
        "<section class='stats' aria-label='Home overview'>",
    ]
    for value, label, detail in (
        (device_count, "Device groups", "Related entities stay together"),
        (sum(bool(area["id"]) for area in areas), "Rooms", f"Across {floor_count} floors"),
        (len(all_entities), "Available entities", "Choose what Alexa can access"),
        (selected_count, "Selected", "Changes take effect after you apply"),
    ):
        identifier = " id='stat-selected'" if label == "Selected" else ""
        content.append(f"<div class='stat'><strong{identifier}>{value}</strong>"
                       f"<span>{label}</span><small>{esc(detail)}</small></div>")
    content.append("</section><section class='link-panel' aria-labelledby='link-title'>"
                   "<div><span class='eyebrow'>ACCOUNT LINKING</span><h2 id='link-title'>A private handshake.</h2>"
                   "<p>Start linking in Alexa, then approve the matching code here.</p></div>"
                   "<div id='link-requests'>")
    content.append(link_cards(links, grants, csrf))
    content.append("</div><p class='poll-error' id='poll-error' role='status' hidden></p></section>")
    if configuration:
        content.append("<section class='notice'><div><strong>Configuration ready to check</strong>"
                       "<p>Validate your applied configuration before restarting Home Assistant.</p></div>" +
                       configuration + "</section>")
    if warning:
        content.append("<section class='notice warning' role='alert'>" + esc(warning) + "</section>")
    if endpoint_status is not None:
        content.append("<section class='notice'><div><strong>Native device endpoints active</strong>"
                       f"<p>{len(endpoint_status['devices'])} Alexa endpoints. Controls and supported sensors "
                       "share one physical-device identity.</p></div></section>")
        if endpoint_status.get("warnings"):
            content.append("<section class='notice warning'><details><summary>Compatibility details</summary><ul>" +
                           "".join("<li>" + esc(value) + "</li>" for value in endpoint_status["warnings"]) +
                           "</ul></details></section>")
    content.append(
        "<form method='post' id='entity-form'>"
        f"<input type='hidden' name='csrf' value='{esc(csrf)}'>"
        "<input type='hidden' name='action' value='preview'>"
        "<div class='workspace'><aside class='rooms'><div class='sidebar-heading'>YOUR SPACES</div>"
        "<button type='button' class='room-button active' data-area='*'>" + icon("home") +
        f"<span>Whole home</span><span class='room-count'>{len(all_entities)}</span></button>")
    floors = defaultdict(list)
    for area in areas:
        floors[area["floor"]].append(area)
    for floor, rooms in floors.items():
        content.append(f"<button type='button' class='floor-label' data-floor='{esc(floor)}'>{esc(floor)}</button>")
        for area in rooms:
            count = sum(len(device["entities"]) for device in area["devices"])
            content.append(f"<button type='button' class='room-button' data-area='{esc(area['id'])}'>"
                           f"<span class='room-dot'></span><span>{esc(area['name'])}</span>"
                           f"<span class='room-count'>{count}</span></button>")
    content.append(
        "<div class='sidebar-note'>" + icon("check") +
        "<p>Rooms and floors come from Home Assistant. Nothing to organize twice.</p></div></aside>"
        "<main class='inventory'><div class='inventory-heading'><div class='eyebrow'>CURATE YOUR CONNECTION</div>"
        "<h2 id='view-title'>Whole home</h2><p>Select physical devices for Alexa. "
        "Their supported controls and sensors become one native endpoint. "
        "Expand your selection only as needed; scenes remain independent.</p></div>"
        "<div class='toolbar'><label class='search'>" + icon("search") +
        "<span class='sr-only'>Search entities, devices, or rooms</span>"
        "<input id='entity-search' type='search' placeholder='Search your home...' autocomplete='off'></label>"
        "<label class='sr-only' for='domain-filter'>Entity type</label>"
        "<select id='domain-filter'><option value='*'>All entity types</option>")
    for domain in sorted({entity["domain"] for entity in all_entities}):
        content.append(f"<option value='{domain}'>{esc(domain.replace('_', ' ').title())}</option>")
    content.append("</select></div><div class='selection-tools'>"
                   "<label class='selected-toggle'><input type='checkbox' id='selected-only'> Selected only</label>"
                   "<span id='visible-count' aria-live='polite'></span>"
                   "<button type='button' class='text-button' id='select-visible'>Select visible</button>"
                   "<button type='button' class='text-button muted' id='clear-visible'>Clear visible</button>"
                   "</div>")
    for area in areas:
        content.append(
            f"<section class='area-section' data-area-id='{esc(area['id'])}' "
            f"data-area-name='{esc(area['name'])}' data-floor-name='{esc(area['floor'])}'><div class='area-heading'>"
            f"<h3>{esc(area['name'])}</h3><span>{esc(area['floor'])}</span></div><div class='device-grid'>")
        for device in area["devices"]:
            search = " ".join([area["name"], area["floor"], device["name"],
                               device["manufacturer"], device["model"]]).casefold()
            main_domain = device["entities"][0]["domain"]
            endpoint = next((endpoint_by_entity[entity["id"]] for entity in device["entities"]
                             if entity["id"] in endpoint_by_entity), None)
            endpoint_label = (
                f"Alexa: {esc(endpoint['name'])} / {len(endpoint['capabilities'])} capabilities"
                if endpoint else (
                    "Independent Alexa endpoints" if not device["physical"] or
                    all(entity["domain"] in ("scene", "script", "camera") for entity in device["entities"])
                    else "One physical device / supported capabilities bundled"))
            content.append(
                f"<article class='device-card' data-search='{esc(search)}'>"
                "<header class='device-heading'><span class='device-icon'>" + icon(main_domain) +
                f"</span><div><h4>{esc(device['name'])}</h4><p>{esc(' '.join(filter(None, [device['manufacturer'], device['model']])) or 'Home Assistant')}</p>"
                f"<p class='endpoint-label'>{endpoint_label}</p></div>"
                f"<label class='device-select'><span class='sr-only'>Select visible entities on {esc(device['name'])}</span>"
                "<input type='checkbox' class='device-checkbox'></label></header><div class='device-entities'>")
            last_kind = None
            for entity in device["entities"]:
                kind = "Sensors" if entity["domain"] in ("sensor", "binary_sensor") else "Controls"
                if kind != last_kind:
                    content.append(f"<div class='entity-kind'>{kind}</div>")
                    last_kind = kind
                checked = " checked" if entity["id"] in selected else ""
                search = (entity["id"] + " " + entity["name"]).casefold()
                unavailable = entity["state"] in ("unavailable", "unknown")
                content.append(
                    f"<label class='entity-row' data-domain='{entity['domain']}' data-search='{esc(search)}'>"
                    f"<input type='checkbox' name='entity' value='{esc(entity['id'])}'{checked}>"
                    f"<span class='entity-copy'><span class='entity-name'>{esc(entity['name'])}</span>"
                    f"<span class='entity-id'>{esc(entity['id'])}</span></span>"
                    f"<span class='entity-state {'unavailable' if unavailable else ''}'>"
                    f"{esc(entity['state'])}{esc(' ' + entity['unit'] if entity['unit'] else '')}</span></label>")
            content.append("</div></article>")
        content.append("</div></section>")
    content.append(
        "<div id='empty-results' class='empty-results' hidden>" + icon("search") +
        "<h3>No matches in this space</h3><p>Try another room or clear your filters.</p></div>"
        "</main></div><footer class='selection-footer'><div><strong><span id='selected-count'>"
        f"{selected_count}</span> entities selected</strong>"
        "<span>Preview first. Apply only when you're ready.</span></div>"
        "<button type='submit' class='primary'>Review configuration " + icon("arrow") +
        "</button></footer></form><footer class='page-footer'>Built around your home. "
        "No public Home Assistant endpoint. <span>AWS usage is metered.</span></footer></div>")
    return document("".join(content), csrf)


def link_cards(links, grants, csrf):
    content = []
    for action, items in (("approve", links), ("revoke", grants)):
        for item in items:
            label = "Approve code" if action == "approve" else "Revoke access"
            code = item.get("code", "Linked account")
            content.append(
                f"<form method='post' class='link-card'><strong>{escape(code)}</strong>"
                f"<small>{escape(item['client'])}</small>"
                f"<input type='hidden' name='csrf' value='{escape(csrf, quote=True)}'>"
                f"<input type='hidden' name='action' value='{action}'>"
                f"<input type='hidden' name='id' value='{escape(item['id'], quote=True)}'>"
                f"<button class='{'primary' if action == 'approve' else 'secondary'}'>{label}</button></form>")
    return "".join(content) or "<p class='link-empty'><span class='status-dot'></span>Waiting for a linking request</p>"
