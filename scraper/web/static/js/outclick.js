(function () {
  "use strict";

  const sentAt = new WeakMap();

  function record(event) {
    // Only activations that actually follow the link. `auxclick` fires for
    // every non-primary button, and button 2 is the context menu — inspecting
    // or copying a link would otherwise count as an outbound click and inflate
    // the very conversion number this exists to measure. Middle-click (1)
    // opens the page in a new tab, so it counts.
    if (event.type === "auxclick" && event.button !== 1) return;
    if (event.type === "click" && event.button !== 0) return;

    const link = event.target.closest && event.target.closest("a[data-outclick]");
    if (!link || !link.href) return;

    // Some browsers emit both auxclick and click for an unusual input device.
    // Count one deliberate activation, not DOM event trivia.
    const now = Date.now();
    if (now - (sentAt.get(link) || 0) < 1000) return;
    sentAt.set(link, now);

    const payload = JSON.stringify({
      community_id: link.dataset.communityId || "",
      url: link.href,
      link_type: link.dataset.linkType || "website"
    });
    try {
      const queued = navigator.sendBeacon && navigator.sendBeacon(
        "/api/outclick", new Blob([payload], {type: "application/json"})
      );
      if (!queued) {
        fetch("/api/outclick", {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: payload, keepalive: true, credentials: "same-origin"
        });
      }
    } catch (_) {
      // Analytics is expendable; the direct href must always win.
    }
  }

  document.addEventListener("click", record, true);
  document.addEventListener("auxclick", record, true);
})();
