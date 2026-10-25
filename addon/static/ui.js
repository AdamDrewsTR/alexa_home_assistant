"use strict";

const form = document.querySelector("#entity-form");
if (form) {
  const rows = [...form.querySelectorAll(".entity-row")];
  const cards = [...form.querySelectorAll(".device-card")];
  const areas = [...form.querySelectorAll(".area-section")];
  const search = document.querySelector("#entity-search");
  const domain = document.querySelector("#domain-filter");
  const selectedOnly = document.querySelector("#selected-only");
  let currentArea = "*";
  let currentFloor = null;

  function update() {
    const query = search.value.trim().toLocaleLowerCase();
    let visible = 0;
    let selected = 0;
    for (const area of areas) {
      const matchesArea = (currentArea === "*" || currentArea === area.dataset.areaId) &&
        (currentFloor === null || currentFloor === area.dataset.floorName);
      for (const card of area.querySelectorAll(".device-card")) {
        const deviceRows = [...card.querySelectorAll(".entity-row")];
        for (const row of deviceRows) {
          const checkbox = row.querySelector("input");
          if (checkbox.checked) selected++;
          const matchesQuery = `${card.dataset.search} ${row.dataset.search}`.toLocaleLowerCase().includes(query);
          row.hidden = !(matchesArea && matchesQuery &&
            (domain.value === "*" || domain.value === row.dataset.domain) &&
            (!selectedOnly.checked || checkbox.checked));
          if (!row.hidden) visible++;
        }
        const shown = deviceRows.filter(row => !row.hidden);
        card.hidden = shown.length === 0;
        card.classList.toggle("has-selection", deviceRows.some(row => row.querySelector("input").checked));
        const selectedInView = shown.filter(row => row.querySelector("input").checked).length;
        const checkbox = card.querySelector(".device-checkbox");
        checkbox.checked = shown.length > 0 && selectedInView === shown.length;
        checkbox.indeterminate = selectedInView > 0 && selectedInView < shown.length;
        for (const heading of card.querySelectorAll(".entity-kind")) {
          let sibling = heading.nextElementSibling;
          let hasVisible = false;
          while (sibling && !sibling.classList.contains("entity-kind")) {
            if (!sibling.hidden) hasVisible = true;
            sibling = sibling.nextElementSibling;
          }
          heading.hidden = !hasVisible;
        }
      }
      area.hidden = ![...area.querySelectorAll(".device-card")].some(card => !card.hidden);
    }
    document.querySelector("#selected-count").textContent = selected;
    document.querySelector("#stat-selected").textContent = selected;
    document.querySelector("#visible-count").textContent = `${visible} shown`;
    document.querySelector("#empty-results").hidden = visible !== 0;
    form.querySelector("button[type=submit]").disabled = selected === 0;
    document.querySelector("#select-visible").disabled = visible === 0;
    document.querySelector("#clear-visible").disabled = visible === 0;
  }

  for (const button of form.querySelectorAll(".room-button")) {
    button.addEventListener("click", () => {
      currentArea = button.dataset.area;
      currentFloor = null;
      for (const floor of form.querySelectorAll("[data-floor]")) {
        floor.classList.remove("active");
        floor.setAttribute("aria-pressed", "false");
      }
      for (const other of form.querySelectorAll(".room-button")) {
        const active = other === button;
        other.classList.toggle("active", active);
        other.setAttribute("aria-pressed", String(active));
      }
      document.querySelector("#view-title").textContent =
        currentArea === "*" ? "Whole home" : areas.find(area => area.dataset.areaId === currentArea).dataset.areaName;
      update();
    });
    button.setAttribute("aria-pressed", String(button.classList.contains("active")));
  }
  for (const button of form.querySelectorAll("[data-floor]")) {
    button.setAttribute("aria-pressed", "false");
    button.addEventListener("click", () => {
      currentArea = "*";
      currentFloor = button.dataset.floor;
      for (const other of form.querySelectorAll(".room-button, [data-floor]")) {
        const active = other === button;
        other.classList.toggle("active", active);
        other.setAttribute("aria-pressed", String(active));
      }
      document.querySelector("#view-title").textContent = currentFloor;
      update();
    });
  }
  for (const card of cards) {
    card.querySelector(".device-checkbox").addEventListener("change", event => {
      for (const row of card.querySelectorAll(".entity-row")) {
        if (!row.hidden) row.querySelector("input").checked = event.target.checked;
      }
      update();
    });
  }
  for (const row of rows) row.querySelector("input").addEventListener("change", update);
  for (const [id, checked] of [["select-visible", true], ["clear-visible", false]]) {
    document.getElementById(id).addEventListener("click", () => {
      for (const row of rows) if (!row.hidden) row.querySelector("input").checked = checked;
      update();
    });
  }
  search.addEventListener("input", update);
  domain.addEventListener("change", update);
  selectedOnly.addEventListener("change", update);
  form.addEventListener("submit", () => {
    const button = form.querySelector("button[type=submit]");
    button.disabled = true;
    button.textContent = "Preparing preview...";
  });
  window.addEventListener("pageshow", update);
  update();
}

const requests = document.querySelector("#link-requests");
if (requests) {
  let previous = null;
  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text) node.textContent = text;
    if (className) node.className = className;
    return node;
  }
  async function poll() {
    try {
      const response = await fetch("ui-state", { cache: "no-store", signal: AbortSignal.timeout(8000) });
      if (!response.ok) throw new Error(`Status HTTP ${response.status}`);
      const state = await response.json();
      const connection = document.querySelector("#connection-status");
      connection.classList.toggle("online", state.connected);
      connection.classList.toggle("offline", !state.connected);
      document.querySelector("#connection-label").textContent = state.connected ? "Relay connected" : "Relay disconnected";
      const signature = JSON.stringify([state.links, state.grants]);
      if (signature !== previous) {
        requests.replaceChildren();
        for (const [action, items] of [["approve", state.links], ["revoke", state.grants]]) {
          for (const item of items) {
            const card = element("form", null, "link-card");
            card.method = "post";
            card.append(element("strong", item.code || "Linked account"), element("small", item.client));
            for (const [name, value] of [
              ["csrf", document.body.dataset.csrf], ["action", action], ["id", item.id],
            ]) {
              const input = element("input");
              input.type = "hidden";
              input.name = name;
              input.value = value;
              card.append(input);
            }
            card.append(element("button", action === "approve" ? "Approve code" : "Revoke access",
              action === "approve" ? "primary" : "secondary"));
            requests.append(card);
          }
        }
        if (!requests.children.length) {
          requests.append(element("p", "Waiting for a linking request", "link-empty"));
        }
        previous = signature;
      }
      document.querySelector("#poll-error").hidden = true;
    } catch (error) {
      const message = document.querySelector("#poll-error");
      message.textContent = `Live status unavailable (${error.message}). Retrying; your selections are preserved.`;
      message.hidden = false;
    } finally {
      window.setTimeout(poll, 5000);
    }
  }
  poll();
}
