// AI Market Research Agent dashboard. No dependencies, no build step.
(() => {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const state = { overview: null, history: [], busy: false, changesLoaded: false, digestsLoaded: false };

  // ------------------------------------------------------------- helpers
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmtDate = (ts) => ts ? new Date(ts * 1000).toLocaleDateString("en-GB",
    { day: "numeric", month: "short", year: "numeric" }) : "never";
  const ago = (ts) => {
    if (!ts) return "never";
    const h = (Date.now() / 1000 - ts) / 3600;
    if (h < 1) return "just now";
    if (h < 48) return `${Math.round(h)}h ago`;
    return `${Math.round(h / 24)} days ago`;
  };
  const SYM = { USD: "$", EUR: "€", GBP: "£" };
  const price = (p) => {
    if (p.price === null || p.price === undefined) return "Custom";
    const n = Number(p.price).toLocaleString("en-US", { maximumFractionDigits: 2 });
    const per = p.period === "month" ? "/mo" : p.period === "year" ? "/yr" : "";
    return `${SYM[p.currency] || p.currency + " "}${n}${p.price ? per : ""}`;
  };
  const KIND = { pricing_change: "Pricing change", page_change: "Page change", new_item: "New post" };

  function inline(text) {
    return esc(text)
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
      .replace(/\[(\d{1,2})\](?!\()/g, '<button class="cite" data-n="$1" type="button">$1</button>');
  }
  function renderMarkdown(text) {
    const out = []; let para = [], items = [], tag = "";
    const fp = () => { if (para.length) out.push(`<p>${para.join("<br>")}</p>`); para = []; };
    const fl = () => { if (items.length) out.push(`<${tag}>${items.join("")}</${tag}>`); items = []; };
    for (const line of String(text).trim().split("\n")) {
      let m;
      if (!line.trim()) { fp(); fl(); }
      else if ((m = line.match(/^\s*[-*•]\s+(.*)/)) || (m = line.match(/^\s*\d+[.)]\s+(.*)/))) {
        const t = /^\s*\d/.test(line) ? "ol" : "ul"; fp(); if (t !== tag) fl(); tag = t;
        items.push(`<li>${inline(m[1])}</li>`);
      } else if ((m = line.match(/^(#{1,6})\s+(.*)/))) {
        fp(); fl(); out.push(m[1].length === 1 ? `<h2>${inline(m[2])}</h2>` : `<h3>${inline(m[2])}</h3>`);
      } else { fl(); para.push(inline(line)); }
    }
    fp(); fl();
    return out.join("");
  }
  async function api(path, opts) {
    const r = await fetch(path, opts);
    const data = await r.json().catch(() => null);
    if (!r.ok) throw new Error((data && data.detail) || `Request failed (${r.status})`);
    return data;
  }

  // ------------------------------------------------------------- tabs
  document.querySelectorAll(".tab").forEach((b) => b.onclick = () => showTab(b.dataset.tab));
  function showTab(name) {
    document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
    document.querySelectorAll(".tab-panel").forEach((p) => p.hidden = p.id !== `tab-${name}`);
    if (name === "changes" && !state.changesLoaded) loadChanges();
    if (name === "digests" && !state.digestsLoaded) loadDigests();
    if (name === "ask") $("#question").focus();
    history.replaceState(null, "", `#${name}`);
  }

  // ------------------------------------------------------------- overview
  async function loadOverview() {
    const o = state.overview = await api("/api/overview");
    const s = o.settings;
    const pill = (on, label) => `<span class="pill ${on ? "ok" : ""}">${on ? "✓" : "○"} ${label}</span>`;
    $("#pills").innerHTML = pill(s.llm, "AI") + pill(s.telegram, "Telegram") + pill(s.sheets, "Sheets") +
      `<span class="pill">Last run: ${o.last_run ? ago(o.last_run.at) : "never"}</span>`;

    const comps = o.competitors;
    const total = comps.reduce((n, c) => n + c.changes, 0);
    const high = comps.reduce((n, c) => n + c.high, 0);
    const sources = comps.reduce((n, c) => n + c.sources.length, 0);
    $("#stats").innerHTML = [
      [comps.length, "competitors monitored"], [sources, "pages & feeds watched"],
      [total, "changes detected"], [high, "high-importance changes"],
    ].map(([n, l]) => `<div class="stat"><div class="num">${n}</div><div class="lbl">${l}</div></div>`).join("");

    const sel = $("#f-competitor");
    sel.innerHTML = '<option value="">All competitors</option>' +
      comps.map((c) => `<option>${esc(c.name)}</option>`).join("");

    const recent = await api("/api/changes?limit=60&importance=high,medium");
    $("#competitors").innerHTML = comps.map((c) => {
      const plans = c.plans.length ? `<table class="plans">${c.plans.map((p) =>
        `<tr><td>${esc(p.name)}</td><td class="lim">${esc(p.limits || "")}</td><td class="price">${esc(price(p))}</td></tr>`).join("")}</table>`
        : '<p class="muted small">No pricing extracted yet.</p>';
      const srcs = c.sources.map((x) => `<li><span class="dot ${x.last_error ? "err" : x.last_checked ? "" : "never"}"
        title="${esc(x.last_error || "")}"></span>${esc(x.type)} · <a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.url.replace(/^https?:\/\//, "").slice(0, 46))}</a></li>`).join("");
      const latest = recent.changes.filter((x) => x.competitor === c.name).slice(0, 2);
      const rec = latest.length ? `<div class="recent">${latest.map((x) =>
        `<div><span class="badge ${x.importance}">${x.importance}</span> <a data-change="${x.id}">${esc(x.summary.slice(0, 110))}</a> <span class="muted">· ${fmtDate(x.effective_at)}</span></div>`).join("")}</div>` : "";
      return `<article class="card comp">
        <div class="comp-head"><h2>${esc(c.name)}</h2><span class="muted small">${c.changes} changes · ${c.high} high</span></div>
        <div class="comp-meta">Last change: ${fmtDate(c.last_change)}${c.pricing_date ? ` · prices as of ${fmtDate(c.pricing_date)}` : ""}</div>
        ${plans}<ul class="src-list">${srcs}</ul>${rec}</article>`;
    }).join("") || '<div class="empty card">No competitors yet. Run <code>python -m agent run</code>.</div>';
  }
  document.addEventListener("click", (e) => {
    const a = e.target.closest("[data-change]");
    if (a) openChange(a.dataset.change);
  });

  // ------------------------------------------------------------- changes
  ["#f-competitor", "#f-importance", "#f-kind"].forEach((id) => $(id).onchange = loadChanges);
  async function loadChanges() {
    state.changesLoaded = true;
    const q = new URLSearchParams({ limit: "300" });
    if ($("#f-competitor").value) q.set("competitor", $("#f-competitor").value);
    if ($("#f-importance").value) q.set("importance", $("#f-importance").value);
    if ($("#f-kind").value) q.set("kind", $("#f-kind").value);
    const { changes } = await api(`/api/changes?${q}`);
    $("#change-count").textContent = `${changes.length} change${changes.length === 1 ? "" : "s"}`;
    $("#timeline").innerHTML = changes.map((c) => `
      <li class="tl-item ${c.importance}" data-change="${c.id}">
        <div class="tl-date">${fmtDate(c.effective_at)}</div>
        <div>
          <div class="tl-head"><strong>${esc(c.competitor)}</strong>
            <span class="badge ${c.importance}">${c.importance}</span>
            <span class="muted">${KIND[c.kind] || c.kind} · ${esc(c.source_type)}</span>
            ${c.origin === "archive" ? '<span class="badge archive">history (archive)</span>' : ""}</div>
          <p class="tl-summary">${esc(c.summary)}</p>
          ${pricingChips(c.pricing_changes)}
          ${c.why ? `<p class="tl-why">${esc(c.why)}</p>` : ""}
        </div>
      </li>`).join("") || '<li class="empty card">No changes match these filters.</li>';
  }
  function pricingChips(list) {
    if (!list || !list.length) return "";
    return `<div>${list.map((p) => `<span class="chip ${p.kind === "price_increase" ? "up" : p.kind === "price_decrease" ? "down" : ""}">${esc(p.text)}</span>`).join("")}</div>`;
  }

  async function openChange(id) {
    const c = await api(`/api/changes/${id}`);
    const planTable = (plans) => plans.length ? `<table class="plans">${plans.map((p) =>
      `<tr><td>${esc(p.name)}</td><td class="lim">${esc(p.limits || "")}</td><td class="price">${esc(price(p))}</td></tr>`).join("")}</table>`
      : '<p class="muted small">-</p>';
    const pricing = c.old_plans.length || c.new_plans.length ? `<div class="compare">
        <div><h4>Before · ${fmtDate(c.old_date)}</h4>${planTable(c.old_plans)}</div>
        <div><h4>After · ${fmtDate(c.new_date)}</h4>${planTable(c.new_plans)}</div></div>` : "";
    const diff = c.diff.map((r) => `<div class="${r.op}">${esc(r.text)}</div>`).join("");
    $("#modal-body").innerHTML = `
      <div class="tl-head"><span class="badge ${c.importance}">${c.importance}</span>
        <span class="muted">${KIND[c.kind] || c.kind} · ${esc(c.source_type)} · ${fmtDate(c.effective_at)}</span>
        ${c.origin === "archive" ? '<span class="badge archive">from the Wayback Machine</span>' : ""}</div>
      <h2>${esc(c.competitor)}: ${esc(c.item_title || KIND[c.kind] || "Change")}</h2>
      <p>${esc(c.summary)}</p>
      ${pricingChips(c.pricing_changes)}
      ${c.why ? `<p class="muted"><strong>Why it matters:</strong> ${esc(c.why)}</p>` : ""}
      ${c.url ? `<p class="small"><a href="${esc(c.url)}" target="_blank" rel="noopener">${esc(c.url)}</a></p>` : ""}
      ${pricing}
      <h4 class="muted small">WHAT CHANGED ON THE PAGE</h4>
      <div class="diff">${diff || '<div class="equal">No text diff stored.</div>'}</div>`;
    $("#modal").hidden = false;
  }
  $("#modal-close").onclick = () => $("#modal").hidden = true;
  $("#modal").onclick = (e) => { if (e.target.id === "modal") $("#modal").hidden = true; };
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("#modal").hidden = true; });

  // ------------------------------------------------------------- digests
  async function loadDigests() {
    state.digestsLoaded = true;
    const { digests } = await api("/api/digests");
    $("#digests").innerHTML = digests.map((d) => `<article class="card digest">
        <p class="muted small">Created ${fmtDate(d.created_at)}${d.sent ? " · sent to Telegram" : ""}</p>
        ${renderMarkdown(d.markdown)}</article>`).join("") ||
      '<div class="empty card">No digests yet. Run <code>python -m agent digest</code>.</div>';
  }

  // ------------------------------------------------------------- ask
  const SUGGESTIONS = [
    "Which competitors changed their pricing in the last 12 months?",
    "How much does each competitor charge for its cheapest paid plan?",
    "What has n8n launched recently?",
    "Compare Zapier and Activepieces pricing",
  ];
  $("#suggestions").innerHTML = SUGGESTIONS.map((q) => `<button type="button">${esc(q)}</button>`).join("");
  $("#suggestions").onclick = (e) => { if (e.target.tagName === "BUTTON") ask(e.target.textContent); };

  const q = $("#question");
  q.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#ask-form").requestSubmit(); }
  });
  q.addEventListener("input", () => { q.style.height = "auto"; q.style.height = Math.min(q.scrollHeight, 120) + "px"; });
  $("#ask-form").onsubmit = (e) => { e.preventDefault(); ask(q.value); };

  function addMsg(role, html) {
    $("#welcome").hidden = true;
    const div = document.createElement("div");
    div.className = `msg ${role}`;
    div.innerHTML = `<div class="bubble">${html}</div>`;
    $("#messages").appendChild(div);
    $("#messages").scrollTop = $("#messages").scrollHeight;
    return div.querySelector(".bubble");
  }

  function renderSources(sources, cited) {
    const list = cited ? sources.filter((s) => cited.includes(s.index)) : sources;
    $("#sources").innerHTML = list.map((s) => `<div class="source-card" data-n="${s.index}">
        <div class="t"><span class="n">${s.index}</span>${s.url ? `<a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.title)}</a>` : esc(s.title)}</div>
        <div class="m">${esc(s.competitor)}${s.date ? " · " + esc(s.date) : ""}</div>
        <div class="s">${esc(s.snippet)}</div></div>`).join("") ||
      '<p class="muted small">No sources for this answer.</p>';
  }
  $("#messages").addEventListener("click", (e) => {
    const b = e.target.closest(".cite");
    if (!b) return;
    document.querySelectorAll(".source-card").forEach((c) => c.classList.toggle("hl", c.dataset.n === b.dataset.n));
    const card = $(`.source-card[data-n="${b.dataset.n}"]`);
    if (card) card.scrollIntoView({ block: "nearest", behavior: "smooth" });
  });

  async function ask(text) {
    text = text.trim();
    if (!text || state.busy) return;
    state.busy = true; $("#send-btn").disabled = true;
    q.value = ""; q.style.height = "auto";
    addMsg("user", esc(text));
    const bubble = addMsg("bot", '<span class="typing">Searching the collected data…</span>');
    let answer = "", sources = [], filters = null;
    try {
      const resp = await fetch("/api/ask/stream", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: text, history: state.history.slice(-6) }),
      });
      if (!resp.ok) throw new Error((await resp.json().catch(() => ({}))).detail || `Error ${resp.status}`);
      const reader = resp.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf("\n\n")) >= 0) {
          const line = buf.slice(0, i); buf = buf.slice(i + 2);
          if (!line.startsWith("data: ")) continue;
          const ev = JSON.parse(line.slice(6));
          if (ev.type === "sources") { sources = ev.sources; filters = ev.filters; renderSources(sources); }
          else if (ev.type === "token") { answer += ev.text; bubble.innerHTML = renderMarkdown(answer); }
          else if (ev.type === "replace") { answer = ev.text; bubble.innerHTML = renderMarkdown(answer); }
          else if (ev.type === "done") { renderSources(sources, ev.cited); }
          else if (ev.type === "error") throw new Error(ev.message);
          $("#messages").scrollTop = $("#messages").scrollHeight;
        }
      }
      const f = filters && [filters.competitors.length ? filters.competitors.join(", ") : "", filters.period].filter(Boolean);
      if (f && f.length) bubble.insertAdjacentHTML("beforeend", `<div class="filters-note">🔎 Searched: ${esc(f.join(" · "))}</div>`);
      state.history.push({ role: "user", content: text }, { role: "assistant", content: answer });
    } catch (err) {
      bubble.innerHTML = `<span style="color:var(--high)">${esc(err.message)}</span>`;
    } finally {
      state.busy = false; $("#send-btn").disabled = false;
    }
  }

  // ------------------------------------------------------------- start
  loadOverview().catch((e) => { $("#competitors").innerHTML = `<div class="empty card">${esc(e.message)}</div>`; });
  const start = location.hash.slice(1);
  if (["changes", "digests", "ask"].includes(start)) showTab(start);
})();
