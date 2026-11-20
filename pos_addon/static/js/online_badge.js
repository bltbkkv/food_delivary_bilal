/* Значок «Онлайн-заказы» на экранах кассы: показывает, сколько новых, и пищит, пока их не приняли.
   Подключение — одна строка перед </body>:  <script src="/static/js/online_badge.js"></script> */
(function () {
  "use strict";
  if (location.pathname.indexOf("/online/") === 0 || window.__onlineBadge) return;
  window.__onlineBadge = true;
  var btn = document.createElement("a");
  btn.href = "/online/";
  btn.style.cssText = "position:fixed;left:12px;bottom:12px;z-index:99999;display:none;align-items:center;gap:8px;" +
    "padding:12px 18px;border-radius:30px;font:bold 17px 'Segoe UI',sans-serif;text-decoration:none;color:#fff;" +
    "background:#1f1f1f;box-shadow:0 4px 14px rgba(0,0,0,.35);";
  document.addEventListener("DOMContentLoaded", function () { document.body.appendChild(btn); });
  if (document.body) document.body.appendChild(btn);

  var ctx = null, last = 0, pulse = false;
  function unlock() { try { ctx = ctx || new (window.AudioContext || window.webkitAudioContext)(); ctx.resume(); } catch (e) {} }
  document.addEventListener("click", unlock, true);
  document.addEventListener("touchstart", unlock, true);
  function beep() {
    try {
      ctx = ctx || new (window.AudioContext || window.webkitAudioContext)();
      if (ctx.state === "suspended") ctx.resume();
      [0, 0.25, 0.5].forEach(function (t, i) {
        var o = ctx.createOscillator(), g = ctx.createGain();
        o.frequency.value = i === 1 ? 1175 : 880; o.connect(g); g.connect(ctx.destination);
        g.gain.setValueAtTime(0.0001, ctx.currentTime + t);
        g.gain.exponentialRampToValueAtTime(0.5, ctx.currentTime + t + 0.02);
        g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + t + 0.22);
        o.start(ctx.currentTime + t); o.stop(ctx.currentTime + t + 0.24);
      });
    } catch (e) {}
  }
  function tick() {
    fetch("/online/poll/").then(function (r) { return r.json(); }).then(function (d) {
      if (!d.configured) { btn.style.display = "none"; return; }
      var orders = d.orders || [], fresh = 0, work = 0;
      orders.forEach(function (o) { if (o.status === "new") fresh++; else if (o.status === "accepted" || o.status === "ready") work++; });
      btn.style.display = "flex";
      if (fresh) {
        btn.textContent = "🌐 Новый онлайн-заказ: " + fresh;
        pulse = !pulse; btn.style.background = pulse ? "#dc3545" : "#b02a37";
        if (Date.now() - last > 4500) { last = Date.now(); beep(); }
      } else {
        btn.textContent = d.ok ? "🌐 Онлайн" + (work ? " · в работе " + work : "") : "🌐 Онлайн: нет связи";
        btn.style.background = d.ok ? "#1f1f1f" : "#6c757d";
      }
    }).catch(function () {});
  }
  tick();
  setInterval(tick, 5000);
})();
