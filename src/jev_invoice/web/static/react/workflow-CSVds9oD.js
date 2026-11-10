(() => {
	let e = document.querySelector("[data-workflow]");
	if (!e) return;
	let t = "http://www.w3.org/2000/svg", n = e.querySelector("[data-tokens]"), r = e.querySelector("[data-live]"), i = document.querySelector("[data-pause]"), a = window.matchMedia("(prefers-reduced-motion: reduce)"), o = JSON.parse(document.getElementById("workflow-data").textContent), s = !1, c = (t) => e.querySelector(`[data-node="${t}"]`), l = (e) => document.getElementById(`e-${e}`), u = {
		received: "arrive",
		read: "read",
		transcribe: "vision",
		extract: "extract",
		document_checks: "doc",
		master_data: "md",
		judgment: "jev",
		coding: "coding",
		decision: "policy",
		outcome: "policy"
	}, d = [["in", "arrive"], ["arrive_read", "read"]], f = [["read_extract", "extract"]], p = [["read_vision", "vision"], ["vision_extract", "extract"]], m = [
		["extract_doc", "doc"],
		["doc_md", "md"],
		["md_jev", "jev"],
		["jev_coding", "coding"],
		["coding_finance", "finance"],
		["finance_tax", "tax"],
		["coding_policy", "policy"]
	], h = [["approved_export", "export"]], g = [[
		"policy_approved",
		"approved",
		"ok"
	], ...h], _ = [[
		"policy_review",
		"review",
		"human"
	], ["review_person", "person"]], v = [
		..._,
		[
			"person_approved",
			"approved",
			"ok"
		],
		...h
	], y = [..._, [
		"person_rejected",
		"rejected",
		"block"
	]], b = [[
		"extract_failed",
		"failed",
		"block"
	]];
	function x() {
		let e = o.split, t = Math.random() < e.scan ? p : f;
		if (Math.random() < e.failed) return [
			...d,
			...t,
			...b
		];
		let n = e.auto / Math.max(1 - e.failed, .01);
		return Math.random() < n ? [
			...d,
			...t,
			...m,
			...g
		] : [
			...d,
			...t,
			...m,
			...Math.random() < e.review_approved ? v : y
		];
	}
	function S(e) {
		if (e === "review" || e === "person") return 3200;
		let t = Object.keys(u).find((t) => u[t] === e), n = t && o.avg_ms[t] || 40;
		return Math.min(3e3, 900 + Math.log10(1 + n) * 450);
	}
	let C = [];
	function w() {
		let e = document.createElementNS(t, "g");
		return e.setAttribute("class", "wf-slip"), e.innerHTML = "<rect x=\"-8\" y=\"-6\" width=\"16\" height=\"12\" rx=\"1.5\"/><path d=\"M-4.5 -2 H4.5 M-4.5 1 H4.5 M-4.5 4 H1.5\"/>", n.appendChild(e), {
			el: e,
			route: x(),
			leg: 0,
			along: 0,
			wait: 0,
			tone: ""
		};
	}
	let T = /* @__PURE__ */ new Map();
	function E(e) {
		let t = c(e);
		t && (t.classList.add("is-hit"), clearTimeout(T.get(e)), T.set(e, setTimeout(() => t.classList.remove("is-hit"), 600)));
	}
	function D(e, t) {
		if (e.wait > 0) return e.wait -= t, e.wait <= 0 && e.el.classList.remove("is-inside"), !0;
		let [n, r, i] = e.route[e.leg], a = l(n), o = a.getTotalLength();
		if (e.along += 55 * t / 1e3, e.along >= o) return E(r), i && (e.tone = i, e.el.setAttribute("class", `wf-slip wf-slip--${i}`)), e.leg += 1, e.along = 0, e.leg >= e.route.length ? !1 : (e.wait = S(r), e.el.classList.add("is-inside"), !0);
		let s = a.getPointAtLength(e.along);
		return e.el.setAttribute("transform", `translate(${s.x.toFixed(1)} ${s.y.toFixed(1)})`), !0;
	}
	let O = 0, k = 1e9;
	function A(e) {
		let t = Math.min(e - (O || e), 250);
		if (O = e, !s && !a.matches) {
			k += t, k > 3200 && C.length < 12 && (C.push(w()), k = 0);
			for (let e = C.length - 1; e >= 0; --e) D(C[e], t) || (C[e].el.remove(), C.splice(e, 1));
		}
		requestAnimationFrame(A);
	}
	let j = {
		review: "pending_review",
		approved: "approved",
		export: "exported",
		rejected: "rejected",
		failed: "failed"
	};
	function M(e) {
		if (typeof e != "number") return "";
		if (e < 1e3) return `${Math.round(e)} ms`;
		let t = e / 1e3;
		return t < 60 ? `${t.toFixed(1)} s` : `${Math.floor(t / 60)} min ${Math.round(t % 60)} s`;
	}
	function N(e, t, n) {
		let r = document.createElement(e);
		return t !== void 0 && (r.textContent = t), n && (r.className = n), r;
	}
	function P() {
		e.querySelectorAll("[data-node]").forEach((e) => {
			let t = e.querySelector("[data-stat]"), n = e.dataset.node;
			j[n] ? t.textContent = `${o.counts[j[n]]} now` : e.dataset.step && o.avg_ms[e.dataset.step] !== void 0 && (t.textContent = `avg ${M(o.avg_ms[e.dataset.step])}`);
		});
		let t = (e) => `${Math.round(e * 100)}%`, n = o.split, r = (t, n) => {
			let r = e.querySelector(`[${t}]`);
			r && (r.textContent = n);
		};
		r("data-split-scan", t(n.scan)), r("data-split-auto", t(n.auto)), r("data-split-review", t(Math.max(0, 1 - n.auto - n.failed))), r("data-split-ok", t(n.review_approved)), r("data-split-failed", t(n.failed)), F();
	}
	function F() {
		let e = document.querySelector("[data-speed]");
		if (!e) return;
		let t = o.speed;
		if (e.hidden = !t, !t) return;
		let n = N("a", `${t.label}${t.number ? ` ${t.number}` : ""}`);
		n.href = t.url, e.replaceChildren("Fastest invoice: ", N("strong", M(t.fastest_ms)), " from arrival to decision (", n, `). Typical: ${M(t.typical_ms)} across ${t.count} invoices.`);
	}
	function I() {
		e.querySelectorAll(".wf-node.is-live").forEach((e) => e.classList.remove("is-live")), r.replaceChildren();
		let n = {};
		o.in_flight.forEach((e) => {
			let i = u[e.desk] || "arrive", a = c(i);
			if (!a) return;
			a.classList.add("is-live");
			let o = (n[i] = (n[i] || 0) + 1) - 1, [, s, l] = a.getAttribute("transform").match(/translate\(([-\d.]+) ([-\d.]+)\)/).map(Number), d = document.createElementNS(t, "g");
			d.setAttribute("class", "wf-marker"), d.setAttribute("transform", `translate(${s} ${l - 46 - o * 24})`);
			let f = e.number || e.label, p = Math.max(60, f.length * 8 + 26), m = document.createElementNS(t, "rect");
			Object.entries({
				x: -p / 2,
				y: -11,
				width: p,
				height: 22,
				rx: 3
			}).forEach(([e, t]) => m.setAttribute(e, t));
			let h = document.createElementNS(t, "circle");
			Object.entries({
				cx: -p / 2 + 11,
				cy: 0,
				r: 3.5
			}).forEach(([e, t]) => h.setAttribute(e, t));
			let g = document.createElementNS(t, "text");
			g.setAttribute("x", -p / 2 + 20), g.setAttribute("y", 4), g.textContent = f, d.append(m, h, g), r.appendChild(d);
		});
	}
	function L() {
		let t = e.querySelector("[data-now]");
		if (!t) return;
		t.replaceChildren(...o.in_flight.length ? o.in_flight.map((e) => {
			let t = N("li"), n = N("a", e.label);
			return n.href = e.url, t.append(n, " ", N("span", e.number, "typed"), N("span", `At step: ${e.desk_title}`, "sub")), t;
		}) : [(() => {
			let e = N("li", "Nothing on the route. ", "muted"), t = N("a", "Upload an invoice");
			return t.href = "/", e.append(t, " to watch it move."), e;
		})()]);
		let n = e.querySelector("[data-recent]");
		o.recent.length && n.replaceChildren(...o.recent.map((e) => {
			let t = N("li"), n = N("a", e.label);
			n.href = e.url;
			let r = N("span", void 0, "sub");
			return r.append(N("span", e.status, `tag tag--${e.tone}`), ` ${e.ago}`), t.append(n, " ", N("span", e.number, "typed"), r), t;
		}));
	}
	async function R() {
		try {
			let t = await fetch(e.dataset.liveUrl, { headers: { Accept: "application/json" } });
			t.ok && (o = await t.json(), P(), I(), L());
		} catch {}
		setTimeout(R, document.hidden ? 1e4 : 3e3);
	}
	i?.addEventListener("click", () => {
		s = !s, e.classList.toggle("is-paused", s), i.setAttribute("aria-pressed", String(s)), i.textContent = s ? "Resume movement" : "Pause movement";
	}), a.matches && i && (i.hidden = !0), I(), requestAnimationFrame(A), setTimeout(R, 3e3);
})(), (() => {
	let e = (e) => document.getElementById(`info-${e.dataset.info}`)?.showModal();
	document.addEventListener("click", (t) => {
		let n = t.target.closest("[data-info]");
		if (n) {
			e(n);
			return;
		}
		t.target instanceof HTMLDialogElement && t.target.close();
	}), document.addEventListener("keydown", (t) => {
		let n = t.target.closest?.("[data-info]");
		n && (t.key === "Enter" || t.key === " ") && (t.preventDefault(), e(n));
	});
})();
//#endregion
