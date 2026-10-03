// Workflow map: sample slips travel the route in the same proportions as real invoices,
// each desk lights up as a slip arrives, and invoices being processed right now are
// marked at their desk. Data refreshes every 3 seconds. Movement stops for people who
// prefer reduced motion, and the Pause button stops it for anyone.
(() => {
  const root = document.querySelector("[data-workflow]");
  if (!root) return;
  const SVG = "http://www.w3.org/2000/svg";
  const tokensLayer = root.querySelector("[data-tokens]");
  const liveLayer = root.querySelector("[data-live]");
  const pauseButton = document.querySelector("[data-pause]");
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  let data = JSON.parse(document.getElementById("workflow-data").textContent);
  let paused = false;

  const node = (key) => root.querySelector(`[data-node="${key}"]`);
  const edge = (id) => document.getElementById(`e-${id}`);
  const STEP_NODE = {
    received: "arrive", read: "read", transcribe: "vision", extract: "extract",
    document_checks: "doc", master_data: "md", judgment: "jev", decision: "policy", outcome: "policy",
  };

  // ── Routes: [edge, desk it arrives at, ink colour from here on] ──────────────
  const START = [["in", "arrive"], ["arrive_read", "read"]];
  const TEXT = [["read_extract", "extract"]];
  const SCAN = [["read_vision", "vision"], ["vision_extract", "extract"]];
  const CHECKS = [["extract_doc", "doc"], ["doc_md", "md"], ["md_jev", "jev"], ["jev_policy", "policy"]];
  const PAID = [["approved_export", "export"], ["export_accounting", "accounting"]];
  const AUTO = [["policy_approved", "approved", "ok"], ...PAID];
  const REVIEW = [["policy_review", "review", "human"], ["review_person", "person"]];
  const REVIEW_OK = [...REVIEW, ["person_approved", "approved", "ok"], ...PAID];
  const REVIEW_NO = [...REVIEW, ["person_rejected", "rejected", "block"]];
  const FAILED = [["extract_failed", "failed", "block"]];

  function pickRoute() {
    const s = data.split;
    const reading = Math.random() < s.scan ? SCAN : TEXT;
    if (Math.random() < s.failed) return [...START, ...reading, ...FAILED];
    const auto = s.auto / Math.max(1 - s.failed, 0.01);
    if (Math.random() < auto) return [...START, ...reading, ...CHECKS, ...AUTO];
    return [...START, ...reading, ...CHECKS, ...(Math.random() < s.review_approved ? REVIEW_OK : REVIEW_NO)];
  }

  // How long a slip stays inside a desk: longer where the real desk is slower.
  function dwellMs(desk) {
    if (desk === "review" || desk === "person") return 3200;
    const step = Object.keys(STEP_NODE).find((key) => STEP_NODE[key] === desk);
    const ms = (step && data.avg_ms[step]) || 40;
    return Math.min(3000, 900 + Math.log10(1 + ms) * 450);
  }

  // ── Slips ───────────────────────────────────────────────────────────────────
  const SPEED = 55; // map units per second: slow enough to read the desks as slips pass
  const slips = [];

  function makeSlip() {
    const g = document.createElementNS(SVG, "g");
    g.setAttribute("class", "wf-slip");
    g.innerHTML = '<rect x="-8" y="-6" width="16" height="12" rx="1.5"/>'
      + '<path d="M-4.5 -2 H4.5 M-4.5 1 H4.5 M-4.5 4 H1.5"/>';
    tokensLayer.appendChild(g);
    return { el: g, route: pickRoute(), leg: 0, along: 0, wait: 0, tone: "" };
  }

  // The desk lights up at once, then fades back (the CSS transition does the fade).
  const flashTimers = new Map();
  function flash(desk) {
    const el = node(desk);
    if (!el) return;
    el.classList.add("is-hit");
    clearTimeout(flashTimers.get(desk));
    flashTimers.set(desk, setTimeout(() => el.classList.remove("is-hit"), 600));
  }

  function step(slip, dt) {
    if (slip.wait > 0) {
      slip.wait -= dt;
      if (slip.wait <= 0) slip.el.classList.remove("is-inside");
      return true;
    }
    const [edgeId, desk, tone] = slip.route[slip.leg];
    const path = edge(edgeId);
    const length = path.getTotalLength();
    slip.along += (SPEED * dt) / 1000;
    if (slip.along >= length) {
      flash(desk);
      if (tone) {
        slip.tone = tone;
        slip.el.setAttribute("class", `wf-slip wf-slip--${tone}`);
      }
      slip.leg += 1;
      slip.along = 0;
      if (slip.leg >= slip.route.length) return false; // reached the end of its route
      slip.wait = dwellMs(desk);
      slip.el.classList.add("is-inside");
      return true;
    }
    const point = path.getPointAtLength(slip.along);
    slip.el.setAttribute("transform", `translate(${point.x.toFixed(1)} ${point.y.toFixed(1)})`);
    return true;
  }

  let last = 0;
  let sinceSpawn = 1e9;
  function frame(now) {
    // Keep real speed on slow screens, but after a hidden tab returns, do not jump ahead.
    const dt = Math.min(now - (last || now), 250);
    last = now;
    if (!paused && !reduceMotion.matches) {
      sinceSpawn += dt;
      if (sinceSpawn > 3200 && slips.length < 12) {
        slips.push(makeSlip());
        sinceSpawn = 0;
      }
      for (let i = slips.length - 1; i >= 0; i -= 1) {
        if (!step(slips[i], dt)) {
          slips[i].el.remove();
          slips.splice(i, 1);
        }
      }
    }
    requestAnimationFrame(frame);
  }

  // ── Live data ───────────────────────────────────────────────────────────────
  const COUNT_NODES = { review: "pending_review", approved: "approved", export: "exported", rejected: "rejected", failed: "failed" };

  function duration(ms) {
    if (typeof ms !== "number") return "";
    if (ms < 1000) return `${Math.round(ms)} ms`;
    const s = ms / 1000;
    return s < 60 ? `${s.toFixed(1)} s` : `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`;
  }

  function el(tag, text, className) {
    const e = document.createElement(tag);
    if (text !== undefined) e.textContent = text;
    if (className) e.className = className;
    return e;
  }

  function renderStats() {
    root.querySelectorAll("[data-node]").forEach((g) => {
      const stat = g.querySelector("[data-stat]");
      const key = g.dataset.node;
      if (COUNT_NODES[key]) stat.textContent = `${data.counts[COUNT_NODES[key]]} now`;
      else if (g.dataset.step && data.avg_ms[g.dataset.step] !== undefined) {
        stat.textContent = `avg ${duration(data.avg_ms[g.dataset.step])}`;
      }
    });
    const pct = (v) => `${Math.round(v * 100)}%`;
    const s = data.split;
    const set = (attr, value) => { const d = root.querySelector(`[${attr}]`); if (d) d.textContent = value; };
    set("data-split-scan", pct(s.scan));
    set("data-split-auto", pct(s.auto));
    set("data-split-review", pct(Math.max(0, 1 - s.auto - s.failed)));
    set("data-split-ok", pct(s.review_approved));
    set("data-split-failed", pct(s.failed));
  }

  function renderLive() {
    root.querySelectorAll(".wf-node.is-live").forEach((g) => g.classList.remove("is-live"));
    liveLayer.replaceChildren();
    const perDesk = {};
    data.in_flight.forEach((item) => {
      const desk = STEP_NODE[item.desk] || "arrive";
      const g = node(desk);
      if (!g) return;
      g.classList.add("is-live");
      const stack = (perDesk[desk] = (perDesk[desk] || 0) + 1) - 1;
      const [, x, y] = g.getAttribute("transform").match(/translate\(([-\d.]+) ([-\d.]+)\)/).map(Number);
      const marker = document.createElementNS(SVG, "g");
      marker.setAttribute("class", "wf-marker");
      marker.setAttribute("transform", `translate(${x} ${y - 46 - stack * 24})`);
      const label = item.number || item.label;
      const width = Math.max(60, label.length * 8 + 26);
      const rect = document.createElementNS(SVG, "rect");
      Object.entries({ x: -width / 2, y: -11, width, height: 22, rx: 3 }).forEach(([k, v]) => rect.setAttribute(k, v));
      const dot = document.createElementNS(SVG, "circle");
      Object.entries({ cx: -width / 2 + 11, cy: 0, r: 3.5 }).forEach(([k, v]) => dot.setAttribute(k, v));
      const text = document.createElementNS(SVG, "text");
      text.setAttribute("x", -width / 2 + 20);
      text.setAttribute("y", 4);
      text.textContent = label; // text, never markup: invoice data is untrusted
      marker.append(rect, dot, text);
      liveLayer.appendChild(marker);
    });
  }

  function renderLists() {
    const now = root.querySelector("[data-now]");
    now.replaceChildren(...(data.in_flight.length ? data.in_flight.map((item) => {
      const li = el("li");
      const a = el("a", item.label);
      a.href = item.url;
      li.append(a, " ", el("span", item.number, "typed"), el("span", `At desk: ${item.desk_title}`, "sub"));
      return li;
    }) : [(() => {
      const li = el("li", "Nothing on the route. ", "muted");
      const a = el("a", "Upload an invoice");
      a.href = "/";
      li.append(a, " to watch it move.");
      return li;
    })()]));

    const recent = root.querySelector("[data-recent]");
    if (!data.recent.length) return;
    recent.replaceChildren(...data.recent.map((item) => {
      const li = el("li");
      const a = el("a", item.label);
      a.href = item.url;
      const sub = el("span", undefined, "sub");
      sub.append(el("span", item.status, `tag tag--${item.tone}`), ` ${item.ago}`);
      li.append(a, " ", el("span", item.number, "typed"), sub);
      return li;
    }));
  }

  async function refresh() {
    try {
      const response = await fetch(root.dataset.liveUrl, { headers: { Accept: "application/json" } });
      if (response.ok) {
        data = await response.json();
        renderStats();
        renderLive();
        renderLists();
      }
    } catch (error) {
      // Network blip: keep the last data and try again on the next tick.
    }
    setTimeout(refresh, document.hidden ? 10000 : 3000);
  }

  pauseButton?.addEventListener("click", () => {
    paused = !paused;
    root.classList.toggle("is-paused", paused);
    pauseButton.setAttribute("aria-pressed", String(paused));
    pauseButton.textContent = paused ? "Resume movement" : "Pause movement";
  });
  if (reduceMotion.matches && pauseButton) pauseButton.hidden = true;

  renderLive();
  requestAnimationFrame(frame);
  setTimeout(refresh, 3000);
})();
