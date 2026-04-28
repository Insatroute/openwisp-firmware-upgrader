// static/firmware-upgrader/js/category-selected-device.js
//
// SelectFilter2.js renames <select id="id_devices"> to "id_devices_from",
// so we only reference "id_devices_from"/"id_devices_to".
// django.jQuery is not available at script evaluation time, so all
// jQuery access is deferred to DOMContentLoaded.

(function () {
  "use strict";

  var _refreshTimer = null;
  var _initialOrgId = null;
  var _initialized = false;

  function getOrgId($) {
    var v = $("#id_organization").val();
    if (!v || v === "None" || v === "null") return null;
    return v;
  }

  function getDevicesEndpoint() {
    var p = window.location.pathname;
    if (p.endsWith("/add/")) {
      return p.replace(/add\/$/, "") + "devices-by-org/";
    }
    return p.replace(/\/[^/]+\/change\/$/, "/devices-by-org/");
  }

  function clearOptions(sel) {
    while (sel.options.length) sel.remove(0);
  }

  function addOption(sel, value, text) {
    sel.add(new Option(text, value, false, false));
  }

  function resetFilterInputs($) {
    $("#id_devices_input").val("");
    $("#id_devices_selected_input").val("");
  }

  function rebuild(results, $) {
    var from = document.getElementById("id_devices_from");
    var to = document.getElementById("id_devices_to");

    if (!from || !to) {
      return;
    }

    // Collect IDs of currently chosen devices so we can preserve them
    var chosenIds = {};
    for (var i = 0; i < to.options.length; i++) {
      chosenIds[to.options[i].value] = to.options[i].text;
    }

    clearOptions(from);
    clearOptions(to);

    // Re-add chosen devices that still belong to the new org
    results.forEach(function (row) {
      if (chosenIds.hasOwnProperty(row.id)) {
        addOption(to, row.id, row.text);
      } else {
        addOption(from, row.id, row.text);
      }
    });

    resetFilterInputs($);

    if (window.SelectBox) {
      SelectBox.init("id_devices_from");
      SelectBox.init("id_devices_to");
      SelectBox.redisplay("id_devices_from");
      SelectBox.redisplay("id_devices_to");
    }

    if (window.SelectFilter) {
      SelectFilter.refresh_icons("id_devices");
    }
  }

  function refreshDevices($) {
    var endpoint = getDevicesEndpoint();
    var orgId = getOrgId($);

    var url = new URL(endpoint, window.location.origin);
    url.searchParams.set("org_id", orgId === null ? "null" : orgId);

    return fetch(url.toString(), {
      headers: { "X-Requested-With": "XMLHttpRequest" },
      credentials: "same-origin",
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        rebuild(data.results || [], $);
      })
      .catch(function () {});
  }

  function debouncedRefresh($) {
    clearTimeout(_refreshTimer);
    _refreshTimer = setTimeout(function () {
      refreshDevices($);
    }, 100);
  }

  document.addEventListener("DOMContentLoaded", function () {
    var $ = django.jQuery;
    if (!$) {
      return;
    }

    // Record the initial org value so we can skip the Select2 init event
    _initialOrgId = getOrgId($);
    _initialized = false;

    function onOrgChange() {
      // Skip the first change event fired by Select2 initialization —
      // Django's filter_horizontal already has the correct initial state.
      if (!_initialized) {
        _initialized = true;
        var currentOrg = getOrgId($);
        if (currentOrg === _initialOrgId) {
          return;
        }
      }
      debouncedRefresh($);
    }

    $("#id_organization")
      .on("change", onOrgChange)
      .on("select2:select", onOrgChange)
      .on("select2:clear", onOrgChange);
  });
})();
