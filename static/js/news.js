// World news tab: GET /api/news (stories, feed status, schedule) and POST /api/news/refresh.
// Text from the feeds is untrusted: everything goes through esc(), and only http(s) links are shown.
(() => {
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
  };
  const F = { group: "", theme: "", region: "", source: "", q: "", hours: 24 };
  try { Object.assign(F, JSON.parse(store.get("newsFilters") || "{}") || {}); } catch { /* ignore */ }
  const save = () => store.set("newsFilters", JSON.stringify(F));
  let data = null, loadedAt = 0, timer = null, box = null, busy = false, note = "";

  const parse = (s) => new Date(String(s).replace(" ", "T"));
  function when(p, now) {
    const t = parse(p), n = parse(now), mins = Math.max(0, Math.round((n - t) / 60000));
    const hhmm = p.slice(11, 16);
    const label = p.slice(0, 10) === now.slice(0, 10) ? hhmm : `${t.toLocaleDateString("en-IN", { weekday: "short" })} ${hhmm}`;
    const ago = mins < 60 ? `${mins} min ago` : mins < 48 * 60 ? `${Math.round(mins / 60)} h ago` : `${Math.round(mins / 1440)} days ago`;
    return [label, ago, mins];
  }
  const matches = (i) => (!F.theme || i.themes.includes(F.theme)) && (!F.region || i.regions.includes(F.region))
    && (!F.source || i.source === F.source) && (!F.q || `${i.title} ${i.summary} ${i.source}`.toLowerCase().includes(F.q.toLowerCase()));

  function item(i, now) {
    if (!/^https?:\/\//i.test(i.link)) return "";
    const [label, ago, mins] = when(i.published, now);
    const tags = i.themes.map((t) => `<span class="ntg">${esc(t)}</span>`).join("")
      + i.regions.map((t) => `<span class="ntg rg">${esc(t)}</span>`).join("");
    return `<article class="nitem"><div class="nwhen"><b>${esc(label)}</b><span class="muted">${ago}</span></div>
      <div><div class="nsrc">${esc(i.source)}${i.official ? '<span class="st off">OFFICIAL</span>' : ""}${mins <= 120 ? '<span class="st new">NEW</span>' : ""}</div>
        <a class="nhl" href="${esc(i.link)}" target="_blank" rel="noopener noreferrer">${esc(i.title)} <span class="ext">↗</span></a>
        ${i.summary ? `<p class="nsum">${esc(i.summary)}</p>` : ""}<div class="ntags">${tags}</div></div></article>`;
  }

  function render() {
    if (!box) return;
    if (!data) { box.innerHTML = '<p class="muted">Loading news…</p>'; return; }
    if (!data.available) { box.innerHTML = `<p class="muted">${esc(data.reason || "News isn't available.")}</p>`; return; }
    const now = data.now, all = data.items || [];
    const econ = all.filter((i) => i.group !== "India markets"), markets = all.filter((i) => i.group === "India markets");
    const c = data.counts || {};
    const on = (data.sources || []).filter((s) => s.on);
    const themeCounts = {};
    for (const i of econ) for (const t of i.themes) themeCounts[t] = (themeCounts[t] || 0) + 1;
    const chips = Object.entries(themeCounts).sort((a, b) => b[1] - a[1])
      .map(([t, n]) => `<button class="nchip" data-theme="${esc(t)}" aria-pressed="${F.theme === t}">${esc(t)} <span class="n">${n}</span></button>`).join("");
    const shownEcon = econ.filter((i) => (!F.group || i.group === F.group) && matches(i));
    const shownMk = markets.filter(matches);
    const sources = [...new Set(all.map((i) => i.source))].sort();
    if (F.source && !sources.includes(F.source)) F.source = "";
    const regions = ["India", "US", "Europe", "China", "Asia", "Latin America", "Global"];
    const upd = data.updated ? `updated ${data.updated.slice(11, 16)}${data.updated.slice(0, 10) !== now.slice(0, 10) ? " " + data.updated.slice(0, 10) : ""}` : "not fetched yet";
    const plan = data.enabled ? `next ${esc(data.next || "—")} (daily at ${esc((data.times || []).join(", "))} IST)` : "scheduled updates are off";
    const srcRows = (data.sources || []).map((s) => {
      const mark = !s.on ? '<span class="muted">off</span>' : s.ok === undefined ? '<span class="muted">·</span>' : s.ok ? '<span class="up">✓</span>' : '<span class="down">✗</span>';
      const detail = !s.on ? "switched off (Settings → World news)" : s.ok === undefined ? "not fetched yet"
        : s.ok ? `${s.items} in the feed, ${s.new} new at ${esc(s.at)}` : `failed at ${esc(s.at)}: ${esc(s.error)}`;
      return `<li>${mark} ${esc(s.name)} <span class="muted">· ${esc(s.group)} · ${esc(s.site)} · ${detail}</span></li>`;
    }).join("");
    box.innerHTML = `
      <div class="nbar">
        <span><b>Economic news</b> · ${all.length} stories in the last ${F.hours >= 48 ? F.hours / 24 + " days" : F.hours + " h"}
          (${c.World || 0} world, ${c.India || 0} India, ${c["India markets"] || 0} India markets) from ${on.length} sources</span>
        <span class="muted">${upd} · ${plan}</span>
        <span class="nright"><select id="nHours" title="How far back"><option value="24">Last 24 h</option><option value="72">Last 3 days</option></select>
          <button id="nRefresh" ${busy ? "disabled" : ""}>${busy ? "Fetching…" : "Refresh now"}</button><span class="muted">${esc(note)}</span></span>
      </div>
      <div class="ndigest"><div class="nlabel">Main themes (counted from the headlines)</div><div class="nchips">${chips || '<span class="muted">none yet</span>'}</div></div>
      <div class="nfilters">
        <span class="nseg" role="group">${[["", "All"], ["World", "World"], ["India", "India"]].map(([g, t]) => `<button data-group="${g}" aria-pressed="${F.group === g}">${t}</button>`).join("")}</span>
        <select id="nRegion"><option value="">All regions</option>${regions.map((r) => `<option ${F.region === r ? "selected" : ""}>${r}</option>`).join("")}</select>
        <select id="nSource"><option value="">All sources</option>${sources.map((s) => `<option ${F.source === s ? "selected" : ""}>${esc(s)}</option>`).join("")}</select>
        <input id="nQ" type="search" placeholder="Search, e.g. tariff, RBI, oil" value="${esc(F.q)}">
        <button id="nClear">Clear</button>
        <span class="muted">${shownEcon.length} of ${econ.length} shown</span>
      </div>
      <div class="nlist">${shownEcon.slice(0, 250).map((i) => item(i, now)).join("") || '<p class="muted">No story matches.</p>'}</div>
      <details class="nmarkets" ${store.get("newsMarketsOpen") === "0" ? "" : "open"}>
        <summary><b>India markets</b> <span class="count">(${F.theme || F.region || F.source || F.q ? `${shownMk.length} of ` : ""}${markets.length})</span>
          <span class="muted">· ${esc((data.sources || []).filter((s) => s.group === "India markets").map((s) => s.name).join(", "))}: mostly news about individual stocks</span></summary>
        <div class="nlist">${shownMk.slice(0, 200).map((i) => item(i, now)).join("") || '<p class="muted">No story matches.</p>'}</div>
      </details>
      <details class="nsources"><summary>Sources <span class="count">(${on.length} on)</span></summary><ul>${srcRows}</ul>
        <p class="muted">Only each publisher's public feed is read: the headline, time and the short summary it puts in the feed. Articles open on the publisher's site.
          Sources and times are on the Settings page (World news). Moneycontrol comes through Google News: its own RSS stopped in 2024.</p></details>
      <p class="muted nfoot">News for context while learning. Headlines and summaries are the publishers' own. Not investment advice.</p>`;
    box.querySelector("#nHours").value = String(F.hours >= 48 ? 72 : 24);
  }

  async function load() {
    try {
      const res = await fetch(`/api/news?hours=${encodeURIComponent(F.hours)}`, { cache: "no-store" });
      data = await res.json();
      loadedAt = Date.now();
    } catch (e) {
      data = data || { available: false, reason: `Couldn't load the news: ${e}` };
    }
    const focused = document.activeElement && document.activeElement.id === "nQ";
    render();
    if (focused) { const q = box && box.querySelector("#nQ"); if (q) { q.focus(); q.setSelectionRange(q.value.length, q.value.length); } }
  }

  function bind(el) {
    el.addEventListener("click", async (e) => {
      const chip = e.target.closest(".nchip");
      if (chip) { F.theme = F.theme === chip.dataset.theme ? "" : chip.dataset.theme; save(); render(); return; }
      const seg = e.target.closest(".nseg button");
      if (seg) { F.group = seg.dataset.group; save(); render(); return; }
      if (e.target.id === "nClear") { Object.assign(F, { group: "", theme: "", region: "", source: "", q: "" }); save(); render(); return; }
      if (e.target.id === "nRefresh" && !busy) {
        busy = true; note = ""; render();
        try {
          const res = await fetch("/api/news/refresh", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
          const r = await res.json();
          note = r.ok ? `${r.new} new` + (r.failed && r.failed.length ? ` · failed: ${r.failed.join(", ")}` : "") : (r.message || r.error || "");
        } catch (err) { note = String(err); }
        busy = false;
        await load();
      }
    });
    el.addEventListener("change", (e) => {
      if (e.target.id === "nRegion") { F.region = e.target.value; save(); render(); }
      if (e.target.id === "nSource") { F.source = e.target.value; save(); render(); }
      if (e.target.id === "nHours") { F.hours = Number(e.target.value); save(); load(); }
    });
    el.addEventListener("input", (e) => {
      if (e.target.id === "nQ") { F.q = e.target.value; save(); clearTimeout(el._t); el._t = setTimeout(() => { render(); const q = box.querySelector("#nQ"); q.focus(); q.setSelectionRange(q.value.length, q.value.length); }, 250); }
    });
    el.addEventListener("toggle", (e) => { if (e.target.classList && e.target.classList.contains("nmarkets")) store.set("newsMarketsOpen", e.target.open ? "1" : "0"); }, true);
  }

  window.NewsTab = {
    show(el) {
      if (box !== el) { box = el; bind(el); }
      el.hidden = false;
      if (!data || Date.now() - loadedAt > 60000) load(); else render();
      if (!timer) timer = setInterval(() => { if (!document.hidden && box && !box.hidden) load(); }, 60000);
    },
    hide() { if (box) box.hidden = true; clearInterval(timer); timer = null; },
    // the dashboard push says when the server last fetched: reload once it's newer than what we show
    updated(hhmm) { if (box && !box.hidden && data && hhmm && hhmm !== (data.updated || "").slice(11, 16)) load(); },
  };
})();
