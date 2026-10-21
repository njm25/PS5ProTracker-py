// Live updates over server-sent events, ticking clocks and countdowns, toasts, and charts.
(() => {
  "use strict";

  // --- live updates: the server says what changed; panels listening for it re-fetch themselves ---
  const conn = document.getElementById("conn");
  let source;
  function connect() {
    source = new EventSource("/api/stream");
    source.onopen = () => setConn(true);
    source.onerror = () => setConn(false);
    for (const name of ["monitor", "job"]) {
      source.addEventListener(name, () => htmx.trigger(document.body, name));
    }
  }
  function setConn(ok) {
    if (!conn) return;
    conn.textContent = ok ? "live" : "reconnecting…";
    conn.classList.toggle("muted", ok);
  }
  connect();
  // A phone that slept drops the stream: reconnect and catch up when the tab comes back.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      if (source.readyState === EventSource.CLOSED) connect();
      htmx.trigger(document.body, "monitor");
    }
  });

  // --- ticking text ---
  function agoText(s) {
    if (s < 0) return "just now";
    if (s < 90) return `${s}s ago`;
    if (s < 5400) return `${Math.floor(s / 60)}m ago`;
    if (s < 172800) return `${Math.floor(s / 3600)}h ago`;
    return `${Math.floor(s / 86400)}d ago`;
  }
  function span(s) {
    if (s <= 0) return "now";
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.floor(s / 60)}m`;
    if (s < 86400) return `${Math.floor(s / 3600)}h`;
    return "∞";
  }
  function tick() {
    const now = Date.now() / 1000;
    for (const el of document.querySelectorAll("[data-ago]")) {
      el.textContent = agoText(Math.floor(now - Number(el.dataset.ago)));
    }
    for (const el of document.querySelectorAll("[data-until]")) {
      el.textContent = span(Math.ceil(Number(el.dataset.until) - now));
    }
    for (const ring of document.querySelectorAll(".ring[data-next]")) {
      const next = Number(ring.dataset.next), total = Number(ring.dataset.total) || 1;
      const left = Math.max(0, next - now);
      const fill = ring.querySelector(".fill");
      const c = Number(fill.dataset.c);
      // A ring just swapped in by a live update jumps straight to where it is; only
      // rings already on screen animate, so one target's check doesn't restart them all.
      const fresh = !fill.dataset.shown;
      if (fresh) fill.style.transition = "none";
      fill.style.strokeDashoffset = String(c * Math.min(1, left / total));
      if (fresh) {
        fill.getBoundingClientRect(); // apply the jump before turning the animation back on
        fill.style.transition = "";
        fill.dataset.shown = "1";
      }
      ring.querySelector("span").textContent = span(Math.ceil(left));
    }
  }
  setInterval(tick, 1000);
  // Before the browser paints the new panel, so fresh rings never show empty for a frame.
  document.addEventListener("htmx:afterSwap", tick);
  document.addEventListener("htmx:afterSettle", (e) => {
    tick();
    // Logs read bottom-up: keep them scrolled to the newest line.
    if (e.target.matches && e.target.matches(".console")) e.target.scrollTop = e.target.scrollHeight;
  });
  tick();

  // --- toasts: HX-Trigger {"toast": {message, ok}} from any action ---
  const toasts = document.getElementById("toasts");
  function toast(message, ok = true) {
    const el = document.createElement("div");
    el.className = "toast" + (ok ? "" : " bad");
    el.textContent = message;
    toasts.appendChild(el);
    setTimeout(() => el.remove(), ok ? 3500 : 7000);
  }
  document.body.addEventListener("toast", (e) => toast(e.detail.message, e.detail.ok));
  document.body.addEventListener("htmx:responseError", (e) => toast(`Request failed (${e.detail.xhr.status})`, false));
  document.body.addEventListener("htmx:sendError", () => toast("Can't reach the server", false));

  // --- confirm buttons: data-confirm="…" needs a second click within 4s ---
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-confirm]");
    if (!btn) return;
    if (btn.dataset.armed) return;  // second click: let htmx have it
    e.preventDefault();
    e.stopImmediatePropagation();
    const label = btn.textContent;
    btn.dataset.armed = "1";
    btn.textContent = btn.dataset.confirm;
    setTimeout(() => { delete btn.dataset.armed; btn.textContent = label; }, 4000);
  }, true);

  // --- charts ---
  const css = getComputedStyle(document.documentElement);
  const color = (name) => css.getPropertyValue(name).trim();
  const axis = () => ({
    stroke: color("--text-3"),
    grid: { stroke: "rgba(255,255,255,.05)", width: 1 },
    ticks: { stroke: "rgba(255,255,255,.08)", width: 1 },
  });

  function sized(el, opts, data) {
    const make = () => new uPlot({ ...opts, width: el.clientWidth, height: opts.height || 240 }, data, el);
    let plot = make();
    new ResizeObserver(() => plot.setSize({ width: el.clientWidth, height: opts.height || 240 })).observe(el);
    return plot;
  }

  // Latency of good checks as a line, failed checks as red dots along the bottom.
  window.latencyChart = async (el, target, hours) => {
    const res = await fetch(`/api/history/${encodeURIComponent(target)}?hours=${hours}`);
    const d = await res.json();
    el.innerHTML = "";
    if (!d.t.length) {
      el.innerHTML = '<div class="feed-empty">No checks in this range yet.</div>';
      return;
    }
    const bad = d.bad.map((v) => (v === null ? null : 0));
    sized(el, {
      height: 240,
      cursor: { points: { size: 6 } },
      legend: { show: true },
      scales: { x: { time: true }, y: { range: (u, lo, hi) => [0, Math.max(100, hi * 1.1)] } },
      axes: [axis(), { ...axis(), values: (u, v) => v.map((x) => `${x}ms`), size: 56 }],
      series: [
        {},
        { label: "latency", stroke: color("--accent"), width: 1.5, fill: "rgba(106,166,255,.08)", spanGaps: false,
          value: (u, v) => (v == null ? "-" : `${v} ms`) },
        { label: "failed", stroke: color("--red"), width: 0, points: { show: true, size: 6, fill: color("--red") },
          value: (u, v, s, i) => (v == null ? "-" : String(d.status[i] || "failed")) },
      ],
    }, [d.t, d.ok, bad]);
  };

  // Phase 1 run: latency per target over the run.
  window.runChart = (el, rows) => {
    if (!rows.length) return;
    const targets = [...new Set(rows.map((r) => r.target))];
    const palette = ["#6aa6ff", "#3ecf7a", "#f0b44c", "#b294ff", "#ff8fb1", "#4dd6d6", "#ff6464", "#c9d36b"];
    const times = rows.map((r) => Date.parse(r.t) / 1000);
    const series = targets.map((t) => rows.map((r) => (r.target === t && r.ms !== "" ? Number(r.ms) : null)));
    sized(el, {
      height: 260,
      scales: { x: { time: true } },
      axes: [axis(), { ...axis(), values: (u, v) => v.map((x) => `${x}ms`), size: 60 }],
      series: [{}, ...targets.map((t, i) => ({
        label: t, stroke: palette[i % palette.length], width: 0,
        points: { show: true, size: 5, fill: palette[i % palette.length] },
      }))],
    }, [times, ...series]);
  };
})();
