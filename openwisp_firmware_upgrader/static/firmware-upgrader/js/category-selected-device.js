// static/firmware-upgrader/js/category-selected-device.js
(function ($) {
  function getOrgId() {
    const v = $("#id_organization").val();
    if (!v || v === "None" || v === "null") return null;
    return v;
  }

  function getDevicesEndpoint() {
    const p = window.location.pathname;

    // /admin/.../category/add/ -> /admin/.../category/devices-by-org/
    if (p.endsWith("/add/")) {
      return p.replace(/add\/$/, "") + "devices-by-org/";
    }

    // /admin/.../category/<id>/change/ -> /admin/.../category/devices-by-org/
    return p.replace(/\/[^/]+\/change\/$/, "/devices-by-org/");
  }

  function clearOptions(sel) {
    while (sel.options.length) sel.remove(0);
  }

  function addOption(sel, value, text) {
    sel.add(new Option(text, value, false, false));
  }

  function resetFilterInputs() {
    $("#id_devices_input").val("");
    $("#id_devices_selected_input").val("");
  }

  function rebuild(results) {
    const original = document.getElementById("id_devices");
    const from = document.getElementById("id_devices_from");
    const to = document.getElementById("id_devices_to");
    if (!original || !from || !to) return;

    clearOptions(original);
    clearOptions(from);
    clearOptions(to);

    results.forEach((row) => {
      addOption(original, row.id, row.text);
      addOption(from, row.id, row.text);
    });

    resetFilterInputs();

    if (window.SelectBox) {
      window.SelectBox.init("id_devices_from");
      window.SelectBox.init("id_devices_to");
      window.SelectBox.redisplay("id_devices_from");
      window.SelectBox.redisplay("id_devices_to");
    }
  }

  function refreshDevices() {
    const endpoint = getDevicesEndpoint();
    const orgId = getOrgId();

    const url = new URL(endpoint, window.location.origin);
    url.searchParams.set("org_id", orgId === null ? "null" : orgId);

    return fetch(url.toString(), {
      headers: { "X-Requested-With": "XMLHttpRequest" },
      credentials: "same-origin",
    })
      .then((r) => r.json())
      .then((data) => rebuild(data.results || []))
      .catch((e) => console.warn("devices-by-org failed:", e));
  }

  $(function () {
    // initial load
    refreshDevices();

    // IMPORTANT: select2 triggers these reliably
    $("#id_organization")
      .on("change", refreshDevices)
      .on("select2:select", refreshDevices)
      .on("select2:clear", refreshDevices);
  });
})(django.jQuery);
