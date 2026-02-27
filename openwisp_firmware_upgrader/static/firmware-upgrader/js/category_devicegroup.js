(function () {
  function $(id) { return document.getElementById(id); }

  function updateGroups(orgId) {
    const groupSelect = $("id_device_group");
    if (!groupSelect) return;

    // clear current options
    groupSelect.innerHTML = "";
    const emptyOpt = document.createElement("option");
    emptyOpt.value = "";
    emptyOpt.textContent = "---------";
    groupSelect.appendChild(emptyOpt);

    if (!orgId) return;

    const url = window.location.pathname.replace(/(add\/|[0-9a-f-]+\/change\/)/, "") + "devicegroups/?org_id=" + orgId;

    fetch(url, { credentials: "same-origin" })
      .then((r) => r.json())
      .then((data) => {
        data.forEach((g) => {
          const opt = document.createElement("option");
          opt.value = g.id;
          opt.textContent = g.name;
          groupSelect.appendChild(opt);
        });
      })
      .catch(() => {});
  }

  document.addEventListener("DOMContentLoaded", function () {
    const orgSelect = $("id_organization");
    if (!orgSelect) return;

    // initial load (use existing org value if editing)
    updateGroups(orgSelect.value);

    // update on change
    orgSelect.addEventListener("change", function () {
      updateGroups(this.value);
    });
  });
})();
