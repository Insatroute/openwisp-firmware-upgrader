  "use strict";

  django.jQuery(function ($) {
    var POLL_INTERVAL = 5000; // 5 seconds
    var timer = null;
    var polling = false;

    // Only run on device change pages
    var match = window.location.pathname.match(
      /\/config\/device\/([a-f0-9-]+)\/change\//
    );
    if (!match) {
      return;
    }

    // Find the upgrade operations inline group
    var $inlineGroup = $("#upgradeoperation_set-group");
    if (!$inlineGroup.length) {
      return;
    }

    function hasActiveOperations() {
      // Django renders choice display values:
      //   "in-progress" -> "in progress", "scheduled" -> "scheduled"
      var activeFound = false;
      $inlineGroup.find(".field-status .readonly").each(function () {
        var statusText = $(this).text().trim().toLowerCase();
        if (statusText === "in progress" || statusText === "scheduled") {
          activeFound = true;
          return false; // break
        }
      });
      return activeFound;
    }

    function refreshUpgradeSection() {
      if (polling) {
        return;
      }
      polling = true;

      // Fetch the page and replace ONLY the Recent Firmware Upgrades
      // inline section. Sidebar, tabs, forms — everything else untouched.
      $.ajax({
        url: window.location.href,
        dataType: "html",
        success: function (html) {
          var $parsed = $("<div>").append($.parseHTML(html));
          var $freshInline = $parsed.find("#upgradeoperation_set-group");
          if ($freshInline.length) {
            // Replace only inner HTML — keeps the outer #upgradeoperation_set-group
            // div intact so the tab system's bindings are not destroyed.
            $inlineGroup.html($freshInline.html());
          }
          if (hasActiveOperations()) {
            scheduleNextPoll();
          } else {
            timer = null;
          }
        },
        error: function () {
          timer = setTimeout(refreshUpgradeSection, POLL_INTERVAL * 2);
        },
        complete: function () {
          polling = false;
        },
      });
    }

    function scheduleNextPoll() {
      if (timer) {
        clearTimeout(timer);
      }
      timer = setTimeout(refreshUpgradeSection, POLL_INTERVAL);
    }

    // Start polling if there are active operations on initial page load
    if (hasActiveOperations()) {
      scheduleNextPoll();
    }
  });
