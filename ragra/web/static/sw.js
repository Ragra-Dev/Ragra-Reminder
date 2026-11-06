/* Ragra service worker: makes the app installable and shows push alerts while the app is closed. */
self.addEventListener("install", function () { self.skipWaiting(); });
self.addEventListener("activate", function (e) { e.waitUntil(self.clients.claim()); });
// A fetch handler is required by Chrome for installability; it just passes requests through.
self.addEventListener("fetch", function () {});

self.addEventListener("push", function (event) {
  var data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { data = { body: event.data ? event.data.text() : "" }; }
  var title = data.title || "Ragra";
  event.waitUntil(self.registration.showNotification(title, {
    body: data.body || "You have a new update.",
    icon: "/static/icons/icon-192.png",
    badge: "/static/icons/badge-96.png",
    data: { url: data.url || "/chat" },
    tag: data.tag || undefined,
  }));
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  var target = (event.notification.data && event.notification.data.url) || "/chat";
  event.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(function (list) {
    for (var i = 0; i < list.length; i++) {
      if ("focus" in list[i]) { list[i].navigate(target); return list[i].focus(); }
    }
    return self.clients.openWindow(target);
  }));
});
