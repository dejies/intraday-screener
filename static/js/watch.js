// Breakout watch tab: GET /api/watch?day=YYYY-MM-DD - the day's coiling / pressing signals with what followed,
// and the hit rate today, over the last 7 days and since the start. Symbols open the stock detail panel
// (app.js listens for clicks on a.sym anywhere on the page).
(() => {
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const num = (v, d = 2) => (v === null || v === undefined || Number.isNaN(v)) ? "—"
    : Number(v).toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });
  const signed = (v, d = 2) => (v === null || v === undefined) ? "—" : `${v > 0 ? "+" : ""}${num(v, d)}%`;
  const pct = (v) => (v === null || v === undefined) ? "—" : `${Math.round(v)}%`;
  const sym = (s) => `<a class="sym" href="#" data-sym="${esc(s)}">${esc(s)}</a>`;
  const KIND = { coiling: ["COILING", "Coiling under a level"], pressing: ["PRESSING", "Pressing a level"] };
  let data = null, loadedAt = 0, timer = null, box = null, day = "", lastCount = null, kind = "";
  try { kind = localStorage.getItem("watchKind") || ""; } catch { /* private mode */ }

  function outcome(s) {
    const broke = s.broke_at ? `broke ${esc(s.broke_at.slice(11, 16))} <span class="muted">after ${num(s.minutes_to_break, 1)} min</span>` : "";
    if (s.outcome === null || s.outcome === undefined) {
      return `${broke ? `<span class="up">${broke}</span> · ` : ""}<span class="muted">measuring, ${num(s.minutes_left, 0)} min left</span>`;
    }
    if (s.outcome === "broke") return `<span class="up">✓ ${broke}</span>`;
    if (s.outcome === "no_break") return `<span class="muted">✗ no break in ${s.window_min || 10} min</span>`;
    return '<span class="muted">no data (left the table / feed gap)</span>';
  }

  function detail(s) {
    if (s.kind === "coiling") {
      return `tested ${s.tests}× (${esc((s.test_times || []).join(", "))}) · candles ${num(s.recent_range)}% vs ${num(s.base_range)}% (${num(s.squeeze_ratio)}×)`;
    }
    return `volume ${num(s.vol_ratio, 1)}× · above VWAP ${num(s.vwap)}${s.tests ? ` · tested ${s.tests}×` : ""}`;
  }

  function statRows(title, st, first) {
    return Object.keys(KIND).map((k, i) => {
      const x = (st || {})[k] || {};
      const hit = x.measured ? `${x.broke} of ${x.measured} · <b>${pct(x.hit_pct)}</b>` : "—";
      const fast = x.measured ? `${x.broke_fast} · ${pct(x.hit_fast_pct)}` : "—";
      return `<tr class="${i === 0 && !first ? "wsep" : ""}">${i === 0 ? `<th rowspan="2" scope="rowgroup">${title}</th>` : ""}
        <td><span class="wk wk-${k}">${KIND[k][0]}</span></td><td>${x.signals || 0}${x.measuring ? ` <span class="muted">(${x.measuring} measuring)</span>` : ""}</td>
        <td>${hit}</td><td>${fast}</td><td>${x.median_minutes !== null && x.median_minutes !== undefined ? num(x.median_minutes, 1) + " min" : "—"}</td>
        <td class="up">${signed(x.avg_gain_pct)}</td><td class="down">${signed(x.avg_drop_pct)}</td></tr>`;
    }).join("");
  }

  function statsTable(stats, mins, days) {
    const fast = (stats.today && stats.today.coiling && stats.today.coiling.fast_minutes) || 5;
    return `<div class="wstats"><table><thead><tr><th></th><th>Signal</th><th>Signals</th><th>Broke out ≤ ${mins} min (hit rate)</th>
      <th>≤ ${fast} min</th><th>Median time</th><th>Avg best</th><th>Avg worst</th></tr></thead><tbody>
      ${statRows("Today", stats.today, true)}${statRows("Last 7 days", stats.week)}${statRows(`Since the start<div class="muted">${days || 0} day${days === 1 ? "" : "s"}</div>`, stats.all)}
      </tbody></table></div>`;
  }

  function render() {
    if (!box) return;
    if (!data) { box.innerHTML = '<p class="muted">Loading Breakout watch…</p>'; return; }
    if (!data.available) { box.innerHTML = `<p class="muted">${esc(data.reason || "Breakout watch isn't available.")}</p>`; return; }
    const c = data.settings || {}, mins = c.watch_outcome_minutes || 10, sigs = data.signals || [];
    const all = (data.stats || {}).all || {};
    const measured = Object.values(all).reduce((n, x) => n + (x.measured || 0), 0);
    const counts = { coiling: 0, pressing: 0, measuring: 0 };
    for (const s of sigs) { counts[s.kind] = (counts[s.kind] || 0) + 1; if (s.outcome === null || s.outcome === undefined) counts.measuring++; }
    const isToday = data.day === data.today;
    const shown = sigs.filter((s) => !kind || s.kind === kind);
    const rows = shown.slice(0, 300).map((s) => `<tr>
        <td>${esc((s.time || s.ts.slice(11)).slice(0, 5))}</td><td>${sym(s.symbol)}<div class="muted">${esc(s.sector || "")}</div></td>
        <td><span class="wk wk-${esc(s.kind)}">${esc((KIND[s.kind] || [s.kind])[0])}</span></td>
        <td>${esc(s.zone || `${s.level_name} ${num(s.level)}`)}</td><td>${num(s.price)}</td>
        <td>${s.dist_pct <= 0.005 ? "at" : num(s.dist_pct) + "%"}</td><td class="wdetail">${detail(s)}</td>
        <td>${outcome(s)}</td><td><span class="up">${signed(s.max_gain_pct)}</span> / <span class="down">${signed(s.max_drop_pct)}</span></td></tr>`).join("");
    box.innerHTML = `
      <div class="wbar">
        <span><b>Breakout watch</b> · ${sigs.length} signal${sigs.length === 1 ? "" : "s"} ${isToday ? "today" : "on " + esc(data.day)}
          (${counts.coiling} coiling, ${counts.pressing} pressing${counts.measuring ? `, ${counts.measuring} still being measured` : ""})</span>
        <span class="muted">${data.enabled ? `from ${esc(data.candles || "1-min candles")}` : "switched off (Settings → Breakout watch): showing saved signals"}</span>
        <span class="wright"><input type="date" id="wDay" value="${esc(data.day)}" max="${esc(data.today)}"><button id="wToday" ${isToday ? "disabled" : ""}>Today</button></span>
      </div>
      ${data.note ? `<p class="banner">${esc(data.note)}</p>` : ""}
      ${statsTable(data.stats || {}, mins, data.days)}
      <p class="muted wfoot">${measured < 30 ? `<b>Only ${measured} signal${measured === 1 ? "" : "s"} measured so far: too few to mean anything. Give it 2–3 weeks.</b> ` : ""}
        A <em>hit</em> = price cleared the level by ${num(c.breakout_buffer_pct)}% (the breakout alert's own rule) within ${mins} min of the signal; “best / worst” are the highest and lowest prices in that time, from the signal price.
        Compare the hit rate with how often a random stock near its high breaks out, and look at the worst move as well as the best: a heads-up that is right half the time but falls further than it rises is not an edge.</p>
      <div class="wfilters"><span class="wseg" role="group" aria-label="Signal">${[["", "All"], ["coiling", "Coiling"], ["pressing", "Pressing"]].map(([k, t]) => `<button data-kind="${k}" aria-pressed="${kind === k}">${t}</button>`).join("")}</span>
        <span class="muted">${shown.length > 300 ? `newest 300 of ${shown.length} shown` : `${shown.length} shown`}${data.total > sigs.length ? ` · the ${sigs.length} newest of ${data.total} were loaded` : ""}</span></div>
      <div class="wtable"><table><thead><tr><th>Time</th><th>Stock</th><th>Signal</th><th>Level</th><th>Price</th><th>Below</th><th>Why</th><th>What followed</th><th>Best / worst</th></tr></thead>
        <tbody>${rows || `<tr><td colspan="9" class="muted">${isToday ? "No stock is coiling under or pressing a level yet today." : "No signals on this day."}</td></tr>`}</tbody></table></div>
      <details class="wabout"><summary>How the signals work</summary>
        <p><span class="wk wk-coiling">COILING</span> (LOW alert): price at most ${num(c.watch_max_dist_pct, 1)}% below a resistance level (opening-range high, previous day high, 52-week high or today's high) that has been tested at least ${c.watch_min_tests} times (a 1-min candle's high within ${num(c.watch_touch_pct)}%), while the last ${c.watch_squeeze_bars} candles shrank to at most ${num(c.watch_squeeze_ratio)}× the size of the 20 before them. Sellers are drying up under the level: breakouts often start from such a squeeze, but just as often the coil breaks down.</p>
        <p><span class="wk wk-pressing">PRESSING</span> (MEDIUM alert): price at most ${num(c.watch_press_dist_pct)}% below the level, the last ${c.watch_press_bars} candles traded at least ${num(c.watch_press_volume, 1)}× the volume of the 20 before them, and price is above VWAP.</p>
        <p>Levels within 0.3% of each other count as one zone. A stock gets at most one signal of a kind per level, and none within ${c.watch_cooldown_min} min of the last; no new signals after ${esc(c.watch_until || "15:10")}. Only stocks that pass the hard filters (price band, ATR, series) are watched. Thresholds are on the Settings page (Breakout watch).</p>
        <p>This is a heads-up to look at the chart, not a reason to buy, and the tool never places orders.</p></details>`;
  }

  async function load() {
    try {
      const res = await fetch(`/api/watch${day ? `?day=${encodeURIComponent(day)}` : ""}`, { cache: "no-store" });
      data = await res.json();
      loadedAt = Date.now();
    } catch (e) {
      data = data || { available: false, reason: `Couldn't load Breakout watch: ${e}` };
    }
    render();
  }

  function bind(el) {
    el.addEventListener("change", (e) => { if (e.target.id === "wDay") { day = e.target.value === (data && data.today) ? "" : e.target.value; load(); } });
    el.addEventListener("click", (e) => {
      if (e.target.id === "wToday") { day = ""; load(); return; }
      const b = e.target.closest(".wseg button");
      if (b) { kind = b.dataset.kind; try { localStorage.setItem("watchKind", kind); } catch { /* private mode */ } render(); }
    });
  }

  window.WatchTab = {
    show(el) {
      if (box !== el) { box = el; bind(el); }
      el.hidden = false;
      if (!data || Date.now() - loadedAt > 30000) load(); else render();
      if (!timer) timer = setInterval(() => { if (!document.hidden && box && !box.hidden) load(); }, 30000);
    },
    hide() { if (box) box.hidden = true; clearInterval(timer); timer = null; },
    // the dashboard push carries today's count: reload at once when a new signal arrives
    updated(brief) {
      const n = brief ? brief.count : null;
      if (box && !box.hidden && !day && n !== null && lastCount !== null && n !== lastCount) load();
      lastCount = n;
    },
  };
})();
