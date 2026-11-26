// Intraday Screener dashboard. Data comes from the server (WebSocket /ws, or /api/state as a fallback);
// the browser only displays it: RVOL, RS, scores and strategy results are all computed server-side.
(() => {
  const POLL_MS = 3000;
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const num = (v, d = 2) => (v === null || v === undefined || Number.isNaN(v)) ? "—"
    : Number(v).toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });
  const signed = (v, d = 2, suffix = "") => (v === null || v === undefined) ? "—"
    : `<span class="${v > 0 ? "up" : v < 0 ? "down" : ""}">${v > 0 ? "+" : ""}${num(v, d)}${suffix}</span>`;

  let CFG = {};
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch {} },
  };

  // ---------- tables ----------
  const voteDot = (v) => `<span class="dot ${v || ""}" title="${v ? v + "ish vote" : "no vote"}"></span>`;
  let SCORE_MAX = 8;
  const scoreBar = (s) => `<span class="score">${Array.from({ length: SCORE_MAX }, (_, i) => `<i class="${i < s ? "on" : ""}"></i>`).join("")}</span>`;
  const boTag = (b, extra = "") => {
    const up = b.direction === "up";
    return `<span class="tag ${up ? "bo-up" : "bo-down"}"${extra}>${esc(b.label)} ${up ? "↑" : "↓"}</span>`;
  };
  const boTags = (r) => {                       // latest level + "+N" so the Symbol column stays narrow
    const bs = r.breakouts || [];
    if (bs.length <= 1) return bs.map((b) => boTag(b)).join("");
    const all = bs.map((b) => `${b.label} ${b.direction === "up" ? "↑" : "↓"}`).join(" · ");
    return boTag(bs[bs.length - 1], ` title="${esc("Levels crossed today: " + all)}"`) + `<span class="tag" title="${esc("Levels crossed today: " + all)}">+${bs.length - 1}</span>`;
  };
  const crossTag = (r) => r.macd_cross ? `<span class="tag ${r.macd_cross === "bullish" ? "bo-up" : "bo-down"}" title="MACD crossed its signal line in the last few bars">× ${r.macd_cross === "bullish" ? "↑" : "↓"}</span>` : "";

  const SERIES_TIP = { BE: "Trade-for-trade: delivery only, no intraday", BZ: "Trade-for-trade: delivery only, no intraday",
    SM: "SME platform (traded in lots)", ST: "SME, trade-for-trade", SZ: "SME, trade-for-trade" };
  const seriesTag = (s) => s && s !== "EQ" ? `<span class="tag" title="${esc(SERIES_TIP[s] || "Not intraday-tradable like EQ")}">${esc(s)}</span>` : "";
  const HZ = ["5m", "15m", "30m", "60m", "day"];
  const hzTip = (o) => o ? HZ.map((k) => `${k} ${o[k] === null || o[k] === undefined ? "n/a" : (o[k] > 0 ? "+" : "") + num(o[k], 2)}`).join(" · ") : "n/a";
  const SECTOR_COLS = [
    ["#", (t) => t.rank ?? "—"],
    ["Sector", (t) => `<strong>${esc(t.sector)}</strong>`],
    ["Measured by", (t) => `<span class="muted" title="${t.benchmark === "index" ? "NSE sector index" : "Median return of the sector's liquid EQ-series stocks (no NSE sector index)"}">${esc(t.basis)}</span>`],
    ["Index", (t) => t.index_ltp ? num(t.index_ltp) : '<span class="muted" title="No NSE sector index: measured by a composite">—</span>'],
    ["Day %", (t) => signed(t.ret.day, 2, "%")],
    ["RS 5m", (t) => signed(t.rs["5m"], 2)],
    ["RS 15m", (t) => signed(t.rs["15m"], 2)],
    ["RS 30m", (t) => signed(t.rs["30m"], 2)],
    ["RS 60m", (t) => signed(t.rs["60m"], 2)],
    ["RS day", (t) => `<strong>${signed(t.rs.day, 2)}</strong>`],
    ["State", (t) => t.state ? `<span class="${t.rs.day > 0 ? "up" : "down"}">${t.rs.day > 0 ? "▲" : "▼"} ${esc(t.state)}</span>` : '<span class="muted">unavailable</span>'],
    ["Stocks ▲/▼", (t) => `<span class="up">${num(t.adv, 0)}</span> / <span class="down">${num(t.dec, 0)}</span> <span class="muted">of ${num(t.stocks, 0)}</span>`],
  ];
  function renderSectors(rows) {
    renderTable($("sectorTable"), rows || [], SECTOR_COLS, "No sector data yet (sectors come from watchlist.csv).",
      (t) => (t.rs.day > 0 ? "out" : t.rs.day < 0 ? "under" : ""));
  }
  const sectorCell = (r) => {
    if (!r.sector) return `<span class="muted" title="${esc(r.industry ? "Dhan industry: " + r.industry : "No sector mapping")}">—</span>`;
    const tip = [r.company, r.sector, r.industry ? `Dhan industry: ${r.industry}` : "", r.sector_source ? `source: ${r.sector_source}` : ""].filter(Boolean).join(" · ");
    return `<span class="sector ${r.sector_source && r.sector_source.startsWith("derived") ? "derived" : ""}" title="${esc(tip)}">${esc(r.sector)}</span>`;
  };
  const barRvolCell = (r) => {
    if (r.bar_rvol === null || r.bar_rvol === undefined) return '<span class="muted">—</span>';
    const t = (r.bar_time || "").slice(11, 16);
    const tip = `${r.bar_rvol_class} · ${t} bar: ${num(r.bar_volume, 0)} vs avg ${num(r.bar_avg_volume, 0)} (bar ${num(r.bar_change_pct)}%)`;
    return `<span class="rv rv-${(r.bar_rvol_class || "").toLowerCase()}" title="${esc(tip)}">${r.bar_spike ? "⚡ " : ""}${num(r.bar_rvol, 2)}×</span>`;
  };
  let EMA_PERIODS = [9, 20, 50, 200];
  const emaCell = (r) => {
    const vals = EMA_PERIODS.map((p) => r[`ema${p}`]);
    if (vals.every((v) => v === null || v === undefined)) return '<span class="muted">—</span>';
    const dots = EMA_PERIODS.map((p, i) => {
      const v = vals[i];
      const side = v === null || v === undefined ? "" : r.ltp > v ? "bull" : r.ltp < v ? "bear" : "";
      return `<span class="dot ${side}" title="EMA${p} ${num(v)} (price ${side === "bull" ? "above" : side === "bear" ? "below" : "n/a"})"></span>`;
    }).join("");
    const stack = r.ema_stack === "bullish" ? '<span class="tag bo-up" title="Stacked up: price > EMA9 > EMA20 > EMA50 > EMA200 (bullish EMA vote)">↑</span>'
      : r.ema_stack === "bearish" ? '<span class="tag bo-down" title="Stacked down: price < EMA9 < EMA20 < EMA50 < EMA200 (bearish EMA vote)">↓</span>' : "";
    const cross = r.ema_cross ? `<span class="tag ${r.ema_cross === "bullish" ? "bo-up" : "bo-down"}" title="EMA9 crossed EMA20 in the last few candles">9×20 ${r.ema_cross === "bullish" ? "↑" : "↓"}</span>` : "";
    const al = r.ema_align || {};
    const tip = EMA_PERIODS.map((p, i) => `EMA${p} ${num(vals[i])}`).join(" · ")
      + `\nEMA9 > EMA20 ${yesNo(al.ema9_gt_ema20)} · EMA20 > EMA50 ${yesNo(al.ema20_gt_ema50)} · price > EMA20 ${yesNo(al.price_gt_ema20)} · EMA20 rising ${yesNo(al.ema20_rising)}`
      + (r.ema20_slope_pct !== null && r.ema20_slope_pct !== undefined ? ` (${num(r.ema20_slope_pct, 3)}% over 3 candles)` : "");
    return `<span title="${esc(tip)}">${dots}</span>${stack}${cross}`;
  };
  let DAILY_EMA_PERIODS = [9, 20, 50, 200];
  let DAILY_CROSS = [50, 200];
  const dailyEmaCell = (r) => {
    const vals = DAILY_EMA_PERIODS.map((p) => r[`dema${p}`]);
    if (vals.every((v) => v === null || v === undefined)) return '<span class="muted">—</span>';
    const dots = DAILY_EMA_PERIODS.map((p, i) => {
      const v = vals[i];
      const side = v === null || v === undefined ? "" : r.ltp > v ? "bull" : r.ltp < v ? "bear" : "";
      return `<span class="dot ${side}" title="Daily EMA${p} ${num(v)} (yesterday ${num(r[`dema${p}_prev`])})"></span>`;
    }).join("");
    const stack = r.dema_stack === "bullish" ? '<span class="tag bo-up" title="Stacked up: price > daily EMA9 > EMA20 > EMA50 > EMA200">↑</span>'
      : r.dema_stack === "bearish" ? '<span class="tag bo-down" title="Stacked down: price < daily EMA9 < EMA20 < EMA50 < EMA200">↓</span>' : "";
    const cross = r.dema_cross ? `<span class="tag ${r.dema_cross === "bullish" ? "bo-up" : "bo-down"}" title="${r.dema_cross === "bullish" ? "Golden" : "Death"} cross: daily EMA${DAILY_CROSS[0]} crossed EMA${DAILY_CROSS[1]} in the last few sessions">${DAILY_CROSS[0]}×${DAILY_CROSS[1]} ${r.dema_cross === "bullish" ? "↑" : "↓"}</span>` : "";
    const tip = DAILY_EMA_PERIODS.map((p, i) => `Daily EMA${p} ${num(vals[i])}`).join(" · ");
    return `<span title="${esc(tip)}">${dots}</span>${stack}${cross}`;
  };
  const NA = (why) => `<span class="muted" title="${esc(why)}">—</span>`;
  const yesNo = (v) => v === null || v === undefined ? "n/a" : v ? "yes" : "no";
  const MACD_STATE = {
    strong_bullish: ["▲", "up", "strong bullish: line above zero and above signal, histogram rising"],
    bullish_weakening: ["↑", "muted", "bullish, weakening: line above zero and above signal, but the histogram is shrinking"],
    early_bullish_reversal: ["↗", "up", "early bullish reversal: line still below zero but above signal (or a fresh bullish cross)"],
    bearish: ["↓", "down", "bearish: histogram below zero, line under its signal"],
    neutral: ["→", "muted", "neutral: histogram exactly zero"],
  };
  const macdCell = (r) => {
    if (r.macd_hist === null || r.macd_hist === undefined) return NA(`MACD needs ${CFG.macd ? CFG.macd[1] + CFG.macd[2] : 35} completed candles`);
    const st = MACD_STATE[r.macd_state] || ["", "muted", ""];
    const st15 = MACD_STATE[r.macd15_state];
    const tip = [`${CFG.interval || 5}-min: ${st[2] || r.macd_state}`, `line ${num(r.macd, 3)} · signal ${num(r.macd_signal, 3)} · hist ${num(r.macd_hist, 3)} (prev ${num(r.macd_hist_prev, 3)})`,
      `above zero ${yesNo(r.macd_above_zero)} · above signal ${yesNo(r.macd_above_signal)} · histogram rising ${yesNo(r.macd_hist_rising)}`,
      `${CFG.macd_context || 15}-min context: ${st15 ? st15[2] : "n/a (needs more 15-min candles)"}${r.macd15_hist !== null && r.macd15_hist !== undefined ? ` (hist ${num(r.macd15_hist, 3)})` : ""}`].join("\n");
    return `<span title="${esc(tip)}">${voteDot(r.votes?.macd)}${signed(r.macd_hist, 2)}<span class="state ${st[1]}">${st[0]}</span></span>${crossTag(r)}`;
  };
  const rsiCell = (r) => {
    if (r.rsi === null || r.rsi === undefined) return NA(`RSI needs ${(CFG.rsi_period || 14) + 1} completed candles`);
    const cls = r.rsi > 70 ? "rsi-ob" : r.rsi <= 30 ? "rsi-os" : "";
    const arrow = r.rsi_slope > 0 ? "↗" : r.rsi_slope < 0 ? "↘" : "";
    const zone = r.rsi > 70 ? "above 70: potentially extended (not an automatic rejection)" : r.rsi >= 55 ? "55–70: the initial preferred bullish zone"
      : r.rsi > 50 ? "above 50: positive momentum bias" : r.rsi <= 30 ? "30 or below: deeply weak, potentially stretched" : "below 50: weaker momentum";
    return `<span class="${cls}" title="${esc(`RSI(${CFG.rsi_period || 14}) ${num(r.rsi, 1)}, ${zone}. Change over 3 candles ${num(r.rsi_slope, 1)}`)}">${num(r.rsi, 0)}${arrow}</span>`;
  };
  const dmiCell = (r) => {
    if (r.adx === null || r.adx === undefined) return NA(`DMI/ADX need ${2 * (CFG.dmi_period || 14)} completed candles`);
    const arrow = r.adx_rising === true ? "↗" : r.adx_rising === false ? "↘" : "";
    const tip = `+DI ${num(r.plus_di, 1)} · −DI ${num(r.minus_di, 1)} · ADX ${num(r.adx, 1)} (${r.adx_rising ? "rising" : r.adx_rising === false ? "falling" : "n/a"}, ${signed(r.adx_slope, 1).replace(/<[^>]+>/g, "")} over 3 candles)${r.adx > (CFG.adx_strong || 25) ? ` · above ${CFG.adx_strong || 25}: stronger trend` : r.trending ? ` · ${CFG.adx_min}–${CFG.adx_strong || 25}: developing trend` : ` · below ${CFG.adx_min}: weak or no trend`}`;
    return `<span title="${esc(tip)}">${voteDot(r.votes?.dmi)}${num(r.plus_di, 0)}/${num(r.minus_di, 0)} · <span class="${r.trending ? "" : "muted"}">${num(r.adx, 0)}${arrow}</span></span>`;
  };
  const vwapCell = (r) => {
    if (r.vwap_dist_pct === null || r.vwap_dist_pct === undefined) return NA("No VWAP yet (session not started)");
    const ev = r.vwap_event === "reclaim" ? `<span class="tag bo-up" title="Price moved back above VWAP (by ${CFG.vwap_buffer_pct}%) and held for ${CFG.vwap_confirm_seconds}s at ${esc(r.vwap_event_time)}">reclaim</span>`
      : r.vwap_event === "loss" ? `<span class="tag bo-down" title="Price fell below VWAP (by ${CFG.vwap_buffer_pct}%) and stayed there ${CFG.vwap_confirm_seconds}s at ${esc(r.vwap_event_time)}">lost</span>` : "";
    const slope = r.vwap_slope_pct;
    const arrow = slope > 0 ? "↗" : slope < 0 ? "↘" : "";
    const tip = `VWAP ${num(r.vwap)} · price ${num(r.ltp)} · VWAP slope over ${CFG.vwap_slope_minutes} min: ${slope === null || slope === undefined ? "n/a" : (slope > 0 ? "+" : "") + num(slope, 3) + "%"}`;
    return `<span title="${esc(tip)}">${voteDot(r.votes?.vwap)}${signed(r.vwap_dist_pct, 2)}<span class="state muted">${arrow}</span></span>${ev}`;
  };
  const rvolTip = (d, what) => {
    if (!d) return `${what}: no data yet`;
    const lack = d.sessions < d.min_sessions ? ` · unavailable: only ${d.sessions} past sessions (needs ${d.min_sessions})` : "";
    return `${what} · ${d.sessions} past sessions${lack}`;
  };
  const dayRvolCell = (r) => {
    const d = r.rvol_detail;
    if (r.rvol === null || r.rvol === undefined) return NA(d ? rvolTip(d, `Cumulative volume to ${d.through}`) : "No candles today yet");
    const tip = rvolTip(d, `Today ${num(d.today, 0)} shares to ${d.through} vs the ${CFG.day_rvol_sessions}-session average ${num(d.baseline, 0)} to the same time`);
    return `<span class="${r.rvol >= (CFG.cum_rvol_threshold || 2.5) ? "rv-hot" : r.active ? "" : "muted"}" title="${esc(tip)}">${num(r.rvol, 2)}×</span>`;
  };
  const slotRvolCell = (r) => {
    const d = r.candle_rvol_detail;
    const live = d && d.live_rvol !== null && d.live_rvol !== undefined ? `\nLIVE / INCOMPLETE ${d.live_bar} candle so far: ${num(d.live_rvol, 2)}× (${num(d.live_volume, 0)} shares) — not a completed-candle signal` : "";
    if (r.candle_rvol === null || r.candle_rvol === undefined) return NA(d ? rvolTip(d, `Same-slot ${CFG.interval}-min RVOL${d.bar ? " for " + d.bar : ""}`) + live : "No completed candle today yet");
    const tip = rvolTip(d, `${d.bar} candle ${num(d.volume, 0)} vs the average of the same slot ${num(d.baseline, 0)}`) + live;
    return `<span class="${r.candle_rvol >= (CFG.candle_rvol_threshold || 2) ? "rv-hot" : ""}" title="${esc(tip)}">${num(r.candle_rvol, 2)}×</span>`;
  };
  const orCell = (r) => {
    if (!r.or_complete) return '<span class="muted" title="Opening range still forming (until 09:30)">…</span>';
    const pos = r.votes?.orb === "bull" ? "above" : r.votes?.orb === "bear" ? "below" : "inside";
    const icon = { above: "▲", below: "▼", inside: "↔" }[pos];
    return `<span title="Price ${pos} the opening range ${num(r.or_low)}–${num(r.or_high)}" class="${pos === "above" ? "up" : pos === "below" ? "down" : "muted"}">${voteDot(r.votes?.orb)}${icon}</span>`;
  };
  // ---------- strategies & setup score ----------
  let META = {};                                 // strategy_meta: key -> {name, short, enabled, gates: [[code, text]]}
  const keyOf = (short) => Object.keys(META).find((k) => META[k].short === short);
  const symLink = (s) => `<a class="sym" href="#" data-sym="${esc(s)}" title="Click for every strategy gate and score component">${esc(s)}</a>`;
  const SCORE_PART_LABEL = { cum_rvol: "Cumulative RVOL", vs_sector: "Stock vs sector", sector_rs: "Sector vs Nifty", macd: "MACD momentum",
    vwap: "VWAP position & slope", adx: "ADX & direction", rsi: "RSI", ema: "EMA alignment" };
  const LABEL_SHORT = { "Strong candidate": ["Strong", "strong"], "Good candidate": ["Good", "good"], "Watchlist": ["Watch", "watch"],
    "Low priority": ["Low", ""], "insufficient data": ["data?", ""] };
  const setupCell = (r) => {
    if (r.setup_score === null || r.setup_score === undefined)
      return NA(r.score_label === "filtered" ? "Not scored: fails a hard filter" : "Not scored: no intraday indicators yet");
    const tip = [`Setup score ${r.setup_score} = ${num(r.score_points, 1)} points of the ${num(r.score_denominator, 0)} that could be scored, × 100`,
      ...Object.entries(r.score_parts || {}).map(([k, [p, m]]) => `${SCORE_PART_LABEL[k] || k}: ${m ? `${num(p, 1)} / ${num(m, 0)}` : "unavailable"}`),
      r.score_label === "insufficient data" ? `Label withheld: fewer than ${CFG.score_min_denominator} points could be scored` : `Label: ${r.score_label}`,
      "A scanner / setup score, not a probability of profit."].join("\n");
    const [short, cls] = LABEL_SHORT[r.score_label] || [r.score_label, ""];
    return `<span title="${esc(tip)}"><span class="sscore">${r.setup_score}</span>${r.score_denominator < 100 ? `<span class="denom">/${num(r.score_denominator, 0)}</span>` : ""}<span class="slabel ${cls}">${esc(short)}</span></span>`;
  };
  const gateName = (key, code) => { const g = META[key] && META[key].gates.find(([c]) => c === code); return g ? g[1] : code; };
  const STATUS_TEXT = { qualified: "qualifies", failed: "fails", blocked: "blocked: data unavailable", filtered: "not evaluated (hard filter)",
    no_data: "not evaluated (no intraday indicators)", disabled: "switched off" };
  const stateTip = (c) => [`First qualified ${c.first}${c.entries > 1 ? ` (entry ${c.entries} today)` : ""}`, `${c.state} since ${c.since}`,
    `Entry price ₹${num(c.entry_price)} · entry score ${c.entry_score ?? "—"} · peak ${c.peak_score ?? "—"}`,
    c.reasons && c.reasons.length ? `Stopped qualifying on: ${c.reasons.join(", ")}` : ""].filter(Boolean).join("\n");
  const stratTags = (r) => Object.entries(META).sort(([, a], [, b]) => a.order - b.order).map(([key, m]) => {
    const v = (r.strat || {})[m.short];
    if (!v) return "";
    const [status, failed, missing] = v;
    const life = r.cand && r.cand[key];
    const cls = life ? life.state : status === "qualified" ? "qualified" : "off";
    const lines = [`${m.name}: ${STATUS_TEXT[status] || status}${life ? ` · candidate ${life.state} since ${life.since}` : ""}`,
      ...failed.map((c) => `✗ ${gateName(key, c)}`), ...missing.map((c) => `? ${gateName(key, c)} (no data)`)];
    return `<span class="stag ${cls}" title="${esc(lines.join("\n"))}">${esc(m.short)}</span>`;
  }).join("") || '<span class="muted">—</span>';
  const STATE_ORDER = { new: 0, active: 1, weakening: 2, removed: 3 };
  let TAB = store.get("tab") || "all";
  const lifeOf = (r) => TAB === "all" || TAB === "votes"
    ? Object.values(r.cand || {}).sort((a, b) => STATE_ORDER[a.state] - STATE_ORDER[b.state])[0]
    : (r.cand || {})[keyOf(TAB)];
  const stateBadge = (c) => !c || c.state === "active" ? ""
    : `<span class="st ${c.state}" title="${esc(stateTip(c))}">${{ new: "NEW", weakening: "weakening", removed: "removed" }[c.state]}</span>`;
  const LEVEL_SHORT = { "OR High": "ORH", "PDH": "PDH", "Consolidation high": "Base" };
  const breakCell = (r) => {
    const b = r.breakout_level;
    if (!b) return '<span class="muted">—</span>';
    return `<span class="tag bo-up" title="${esc(`${b.name} ${num(b.level)}: first 5-min close above it on the ${b.bar} candle, ${num(b.age_min, 0)} min ago`)}">${esc(LEVEL_SHORT[b.name] || b.name)} ${esc(b.bar)}</span>`;
  };

  const COLS = [
    ["#", (r) => r.rank ?? ""],
    ["Symbol", (r) => `${symLink(r.symbol)}${r.top ? '<span class="tag top">TOP</span>' : ""}${seriesTag(r.series)}${boTags(r)}`],
    ["Sector", sectorCell],
    ["Vote score", (r) => `<span title="${r.score} of ${SCORE_MAX}: directional votes + the ADX and RVOL strength points">${r.score}${scoreBar(r.score)}</span>`],
    ["LTP", (r) => num(r.ltp)],
    ["Chg %", (r) => signed(r.chg_pct, 2, "%")],
    ["RS", (r) => signed(r.rs, 2)],
    ["Sector RS", (r) => `<span title="${esc("Sector vs NIFTY 50 · " + hzTip(r.sector_rs))}">${signed(r.sector_rs && r.sector_rs.day, 2)}</span>`],
    ["vs Sector", (r) => `<span title="${esc("Stock vs its sector · " + hzTip(r.vs_sector) + (r.sector_pctile !== null && r.sector_pctile !== undefined ? ` · percentile ${num(r.sector_pctile, 0)} of ${r.sector_peer_count}` : ""))}">${signed(r.vs_sector && r.vs_sector.day, 2)}${r.sector_pctile !== null && r.sector_pctile !== undefined ? `<span class="pct">P${num(r.sector_pctile, 0)}</span>` : ""}</span>`],
    ["VWAP %", vwapCell],
    ["EMA 5m", emaCell],
    ["EMA day", dailyEmaCell],
    ["MACD", macdCell],
    ["RSI", rsiCell],
    ["DMI · ADX", dmiCell],
    ["RVOL day · 5m", (r) => `${dayRvolCell(r)} · ${slotRvolCell(r)}`],
    ["Bar RVOL", barRvolCell],
    ["OR", orCell],
  ];
  const NEUTRAL_COLS = [...COLS.filter(([h]) => h !== "#"), ["ATR %", (r) => num(r.atr_pct, 1)], ["Why here", (r) => r.passes_filters
    ? `<span class="muted">mixed: ${r.bull_votes} bull / ${r.bear_votes} bear</span>`
    : `<span class="muted">${esc(r.filter_reasons.join("; "))}</span>`]];

  const col = (h) => COLS.find(([x]) => x === h);
  const CAND_COLS = [
    ["#", (r) => r._rank ?? ""],
    ["Symbol", (r) => `${symLink(r.symbol)}${seriesTag(r.series)}${stateBadge(lifeOf(r))}`],
    col("Sector"),
    ["Setup score", setupCell],
    ["Strategies", stratTags],
    col("LTP"), col("Chg %"), col("RVOL day · 5m"), col("vs Sector"), col("Sector RS"),
    col("MACD"), col("RSI"), col("DMI · ADX"), col("VWAP %"), col("EMA 5m"),
    ["Break", breakCell],
    ["Since", (r) => { const c = lifeOf(r); return c ? `<span title="${esc(stateTip(c))}">${esc(c.first)}</span>` : "—"; }],
  ];

  // ---------- tables: rows keyed by symbol; only cells whose content changed are rewritten ----------
  const SORT = (() => { try { return JSON.parse(store.get("tableSort") || "{}") || {}; } catch { return {}; } })();
  let SELECTED = null;
  const hz = (o, k) => (o ? o[k] : null);
  const SORTERS = {
    "#": (r) => r._rank ?? r.rank, "Symbol": (r) => r.symbol, "Sector": (r) => r.sector, "Vote score": (r) => r.score,
    "Setup score": (r) => r.setup_score, "Setup": (r) => r.setup_score, "Strategies": (r) => (r.strategy_tags || []).length,
    "LTP": (r) => r.ltp, "Chg %": (r) => r.chg_pct, "RS": (r) => r.rs, "Sector RS": (r) => hz(r.sector_rs, "day"),
    "vs Sector": (r) => hz(r.vs_sector, "day"), "VWAP %": (r) => r.vwap_dist_pct, "MACD": (r) => r.macd_hist,
    "RSI": (r) => r.rsi, "DMI · ADX": (r) => r.adx, "RVOL day · 5m": (r) => r.rvol, "Bar RVOL": (r) => r.bar_rvol,
    "ATR %": (r) => r.atr_pct, "Bias": (r) => r.bias, "Break": (r) => (r.breakout_level ? -r.breakout_level.age_min : null),
    "Since": (r) => { const c = lifeOf(r); return c ? c.first : null; },
    "Index": (t) => t.index_ltp, "Day %": (t) => hz(t.ret, "day"), "RS 5m": (t) => hz(t.rs, "5m"), "RS 15m": (t) => hz(t.rs, "15m"),
    "RS 30m": (t) => hz(t.rs, "30m"), "RS 60m": (t) => hz(t.rs, "60m"), "RS day": (t) => hz(t.rs, "day"),
    "Stocks ▲/▼": (t) => (t.adv || 0) - (t.dec || 0), "Measured by": (t) => t.benchmark,
  };
  const TEXT_SORT = new Set(["Symbol", "Sector", "Bias", "Since", "Measured by"]);
  function sortRows(id, rows) {
    const s = SORT[id], f = s && SORTERS[s.col];
    if (!f) return rows;
    return rows.map((r, i) => [r, f(r), i]).sort((p, q) => {
      const a = p[1], b = q[1], an = a === null || a === undefined || a === "", bn = b === null || b === undefined || b === "";
      if (an || bn) return (an - bn) || (p[2] - q[2]);                     // missing values always last
      const c = typeof a === "string" ? a.localeCompare(b) : a - b;
      return (c * s.dir) || (p[2] - q[2]);
    }).map((p) => p[0]);
  }
  const rowKey = (r) => r.symbol ?? r.sector;
  const rowTip = (r) => `OR ${num(r.or_high)} / ${num(r.or_low)} · PDH ${num(r.pdh)} · PDL ${num(r.pdl)} · Gap ${num(r.gap_pct)}% · 52W ${num(r.w52_low)}–${num(r.w52_high)} · ATR ${num(r.atr_pct, 1)}% daily, ${num(r.atr_intraday_pct, 2)}% per ${CFG.interval || 5}-min candle · Turnover ₹${num(r.avg_turnover_cr, 0)} cr`
    + `\nPivot ${num(r.pivot)} · CPR ${num(r.cpr_bc)}–${num(r.cpr_tc)} (${num(r.cpr_width_pct, 2)}%) · R1 ${num(r.r1)} S1 ${num(r.s1)} · R2 ${num(r.r2)} S2 ${num(r.s2)}`
    + `\nBollinger(${(CFG.bb || [20, 2]).join(", ")}) width ${num(r.bb_bandwidth, 2)}% · %B ${num(r.bb_pct_b, 2)}${r.bb_squeeze ? " · SQUEEZE" : ""} · OBV ${r.obv_trend || "n/a"} · MFI ${num(r.mfi, 0)} · last candle ${r.last_bar}\nClick the row for the chart and details.`;
  function renderTable(el, rows, cols, emptyMsg, rowClass) {
    const id = el.id;
    rows = sortRows(id, rows);
    const s = SORT[id];
    const sig = cols.map(([h]) => h).join("|") + (s ? `|${s.col}|${s.dir}` : "");
    if (el._sig !== sig) {                          // columns or sort changed: rebuild the header once
      el.innerHTML = `<thead><tr>${cols.map(([h]) => {
        const can = !!SORTERS[h], arrow = s && s.col === h ? (s.dir > 0 ? " ▲" : " ▼") : "";
        return `<th${can ? ` class="sortable" data-sort="${esc(h)}" title="Sort by ${esc(h)} (again: reverse, third time: default order)"` : ""}>${h}${arrow}</th>`;
      }).join("")}</tr></thead><tbody></tbody>`;
      el._sig = sig; el._rows = new Map();
    }
    const tbody = el.tBodies[0];
    if (!rows.length) {
      tbody.innerHTML = `<tr><td class="empty" colspan="${cols.length}">${emptyMsg}</td></tr>`;
      el._rows = new Map();
      return;
    }
    if (!el._rows.size) tbody.innerHTML = "";
    const next = new Map();
    rows.forEach((r, i) => {
      const k = rowKey(r);
      if (next.has(k)) return;
      let tr = el._rows.get(k);
      if (!tr) {
        tr = document.createElement("tr");
        tr.dataset.key = k;
        cols.forEach(() => tr.appendChild(document.createElement("td")));
      }
      cols.forEach(([, f], j) => {
        const html = f(r), td = tr.children[j];
        if (td._html !== html) { td.innerHTML = html; td._html = html; }
      });
      const cls = [(rowClass ? rowClass(r) : r.top ? "top" : ""), r.symbol && recentAlert(r.symbol) ? "flash" : "",
        r.symbol && r.symbol === SELECTED ? "selected" : ""].filter(Boolean).join(" ");
      if (tr.className !== cls) tr.className = cls;
      const tip = r.symbol ? rowTip(r) : "";
      if (tr._tip !== tip) { tr.title = tip; tr._tip = tip; }
      next.set(k, tr);
      if (tbody.children[next.size - 1] !== tr) tbody.insertBefore(tr, tbody.children[next.size - 1] || null);
    });
    el._rows.forEach((tr, k) => { if (!next.has(k)) tr.remove(); });
    el._rows = next;
  }
  document.addEventListener("click", (e) => {
    const th = e.target.closest("th[data-sort]");
    if (!th) return;
    const id = th.closest("table").id, col = th.dataset.sort, cur = SORT[id];
    const first = TEXT_SORT.has(col) ? 1 : -1;
    if (!cur || cur.col !== col) SORT[id] = { col, dir: first };
    else if (cur.dir === first) SORT[id] = { col, dir: -first };
    else delete SORT[id];
    store.set("tableSort", JSON.stringify(SORT));
    rerender();
  });
  document.addEventListener("click", (e) => {
    if (e.target.closest("a, button, input, select, th, label")) return;
    const tr = e.target.closest("tr[data-key]");
    const table = tr && tr.closest("table");
    if (!table || table.id === "sectorTable") return;
    openDetail(tr.dataset.key);
  });

  // ---------- alert centre ----------
  const SEV = ["CRITICAL", "HIGH", "MEDIUM", "LOW"];
  const SEV_RANK = Object.fromEntries(SEV.map((s, i) => [s, i]));
  let KINDS = {}, RULES = {}, DISCLAIMER = "", TODAY = null, NOW_IST = null;
  const ALERTS = new Map();                      // today's alerts by id (newest arrive via ?alerts_since)
  let historyRows = null;                        // another day's alerts, fetched on demand
  let lastAlertId = null, alertVersion = null, audioCtx = null, unseen = 0;
  const RECENT = new Map();                      // symbol -> time of its latest setup alert (row highlight)
  const recentAlert = (sym) => { const t = RECENT.get(sym); return t && Date.now() - t < 120000; };

  // Per-viewer preferences (this browser only). Everything is always kept in the alert centre.
  const DEFAULT_PREFS = {
    toastMin: "MEDIUM", notifyMin: "HIGH", sound: false, confirmedOnly: false,
    toast: { strategy_qualified: true, score_cross: true, breakout: true, rvol: true, feed: true, auth: true, data_error: true, token: true, refresh: true, sleep: true },
    soundKinds: { strategy_qualified: true, breakout: true, feed: true, auth: true, token: true, refresh: true, sleep: true },
  };
  function loadPrefs() {
    let p = {};
    try { p = JSON.parse(store.get("alertPrefs") || "{}") || {}; } catch {}
    const out = { ...DEFAULT_PREFS, ...p, toast: { ...DEFAULT_PREFS.toast, ...(p.toast || {}) },
      soundKinds: { ...DEFAULT_PREFS.soundKinds, ...(p.soundKinds || {}) } };
    if (!store.get("alertPrefs")) {                // carry over the old header checkboxes
      if (store.get("rvolPopups") === "0") out.toast.rvol = false;
      if (store.get("confirmedOnly") === "1") out.confirmedOnly = true;
    }
    return out;
  }
  let PREFS = loadPrefs();
  const savePrefs = () => store.set("alertPrefs", JSON.stringify(PREFS));

  const checksHtml = (a) => `<div class="checks">${Object.entries(a.confirmations).map(([k, ok]) => `<span class="${ok ? "ok" : "no"}">${ok ? "✓" : "✗"} ${k}</span>`).join("")}</div>`;
  const sevChip = (s) => `<span class="sev sev-${esc((s || "").toLowerCase())}">${esc(s)}</span>`;
  const kindLabel = (a) => a.label || (KINDS[a.kind] || [a.kind])[0];
  const isSystem = (a) => a.category === "system";
  const stratName = (a) => a.strategy_name || (META[a.strategy] || {}).name || "";
  const VALUE_FIELDS = [["ltp", "LTP", 2], ["chg_pct", "Change %", 2], ["rvol", "Day RVOL ×", 2], ["candle_rvol", "5m RVOL ×", 2],
    ["vs_sector", "Stock vs sector", 2], ["sector_rs", "Sector RS", 2], ["rs", "RS vs Nifty", 2], ["macd_hist", "MACD histogram", 3],
    ["macd_state", "MACD state"], ["rsi", "RSI", 1], ["adx", "ADX", 1], ["plus_di", "+DI", 1], ["minus_di", "−DI", 1],
    ["vwap_dist_pct", "VWAP distance %", 2], ["vwap_slope_pct", "VWAP slope %", 3], ["setup_score", "Setup score", 0],
    ["score_label", "Score label"], ["sector_pctile", "Sector percentile", 0], ["sector_ret", "Sector return %", 2], ["rank", "Sector rank", 0]];
  const valuesTip = (a) => {
    const v = a.values || {};
    const lines = VALUE_FIELDS.filter(([k]) => v[k] !== null && v[k] !== undefined)
      .map(([k, label, d]) => `${label}: ${d === undefined ? String(v[k]).replace(/_/g, " ") : num(v[k], d)}`);
    if (a.reasons && a.reasons.length) lines.push(`Reason codes: ${a.reasons.join(", ")}`);
    if (a.examples && a.examples.length) lines.push(`Examples: ${a.examples.join(", ")}`);
    return [`Values when it fired (${a.time} IST):`, ...lines].join("\n");
  };

  function alertRow(a) {
    const sym = a.symbol ? `${symLink(a.symbol)}${seriesTag(a.series)}` : a.sector ? `<span class="muted">${esc(a.sector)}</span>` : "—";
    const conf = a.confirmations && Object.keys(a.confirmations).length ? checksHtml(a) : "";
    const count = a.count > 1 ? ` <span class="tag">×${a.count}</span>` : "";
    const ack = a.acknowledged
      ? `<span class="muted" title="Acknowledged ${esc(a.acknowledged_at || "")}">✓ ${esc((a.acknowledged_at || "").slice(11, 16))}</span>`
      : `<button class="ackbtn" data-ack="${a.id}">Ack</button>`;
    return `<tr class="${isSystem(a) ? "sys" : ""}${a.acknowledged ? " acked" : ""}"><td>${esc(a.time)}</td><td>${sevChip(a.severity)}</td>
      <td>${isSystem(a) ? '<span class="tag systag">SYSTEM</span> ' : ""}${esc(kindLabel(a))}</td><td>${sym}</td><td>${esc(stratName(a))}</td>
      <td class="msg" title="${esc(valuesTip(a))}">${esc(a.text || "")}${count}${conf}</td><td>${ack}</td></tr>`;
  }
  function filters() {
    return { sev: SEV.filter((s) => { const el = $(`af${s}`); return !el || el.checked; }), category: $("afCategory").value,
      strategy: $("afStrategy").value, symbol: $("afSymbol").value.trim().toUpperCase(), day: $("afDay").value, unacked: $("afUnacked").checked };
  }
  const matches = (a, f) => f.sev.includes(a.severity) && (!f.category || a.category === f.category)
    && (!f.strategy || a.strategy === f.strategy) && (!f.symbol || (a.symbol || "").includes(f.symbol)) && (!f.unacked || !a.acknowledged);
  const alertSource = (f) => (f.day && f.day !== TODAY ? (historyRows || []) : [...ALERTS.values()])
    .sort((a, b) => (b.ts || "").localeCompare(a.ts || "") || b.id - a.id);      // newest event first
  function renderAlertCentre() {
    const f = filters();
    const src = alertSource(f);
    const rows = src.filter((a) => matches(a, f));
    $("alertCount").textContent = `(${num(rows.length, 0)}${rows.length !== src.length ? ` of ${num(src.length, 0)}` : ""}${f.day && f.day !== TODAY ? ` on ${f.day}` : " today"})`;
    $("alertTable").innerHTML = rows.length
      ? `<thead><tr><th>Time</th><th>Severity</th><th>Type</th><th>Symbol</th><th>Strategy</th><th>Why it fired (hover for the values)</th><th></th></tr></thead><tbody>${rows.slice(0, 400).map(alertRow).join("")}</tbody>`
      : `<tr><td class="empty">No alerts${src.length ? " match these filters" : " yet"}.</td></tr>`;
  }
  function renderBadge() {
    const c = { CRITICAL: 0, HIGH: 0, MEDIUM: 0, LOW: 0 };
    for (const a of ALERTS.values()) if (!a.acknowledged && c[a.severity] !== undefined) c[a.severity]++;
    const total = SEV.reduce((s, k) => s + c[k], 0);
    $("alertBadgeText").textContent = total
      ? [c.CRITICAL ? `${c.CRITICAL} critical` : "", c.HIGH ? `${c.HIGH} high` : "", `${total} unread`].filter(Boolean).join(" · ") : "no new alerts";
    $("alertBadge").className = `badge-btn ${c.CRITICAL ? "crit" : c.HIGH ? "high" : ""}`;
    const tabN = document.querySelector('#tabs button[data-tab="alerts"] .n');
    if (tabN) tabN.textContent = total;
  }
  async function syncToday() {
    if (!TODAY) return;
    try {
      const list = await (await fetch(`/api/alerts?day=${TODAY}&limit=2000`, { cache: "no-store" })).json();
      ALERTS.clear();
      list.forEach((a) => ALERTS.set(a.id, a));
    } catch {}
    renderAlertCentre(); renderBadge();
  }
  async function loadHistory() {
    const f = filters();
    store.set("alertFilters", JSON.stringify({ sev: f.sev, category: f.category, unacked: f.unacked }));
    if (!f.day || f.day === TODAY) { historyRows = null; renderAlertCentre(); return; }
    try { historyRows = await (await fetch(`/api/alerts?day=${f.day}&limit=2000`, { cache: "no-store" })).json(); } catch { historyRows = []; }
    renderAlertCentre();
  }
  function buildFilters() {
    let saved = {};
    try { saved = JSON.parse(store.get("alertFilters") || "{}") || {}; } catch {}
    $("afSev").innerHTML = SEV.map((s) => `<label><input type="checkbox" id="af${s}" ${!saved.sev || saved.sev.includes(s) ? "checked" : ""}> ${sevChip(s)}</label>`).join(" ");
    if (saved.category) $("afCategory").value = saved.category;
    $("afUnacked").checked = !!saved.unacked;
    ["afCategory", "afStrategy", "afUnacked", "afDay", ...SEV.map((s) => `af${s}`)].forEach((id) => $(id).addEventListener("change", loadHistory));
    $("afSymbol").addEventListener("input", renderAlertCentre);
  }
  buildFilters();

  function ingestAlerts(s) {
    const m = s.alert_meta || {};
    NOW_IST = (s.now || "").slice(11, 19);
    KINDS = m.kinds || KINDS; RULES = m.rules || RULES; DISCLAIMER = m.disclaimer || DISCLAIMER;
    $("alertDisclaimer").textContent = DISCLAIMER;
    if ($("afStrategy").options.length <= 1 && Object.keys(META).length) {
      $("afStrategy").innerHTML = '<option value="">All strategies</option>' + Object.entries(META)
        .sort(([, a], [, b]) => a.order - b.order).map(([k, v]) => `<option value="${esc(k)}">${esc(v.name)}</option>`).join("");
    }
    const day = s.session_day ? String(s.session_day).slice(0, 10) : TODAY;
    if (day && day !== TODAY) {
      const first = TODAY === null;
      TODAY = day; ALERTS.clear();
      if (first || !$("afDay").value) $("afDay").value = TODAY;
    }
    const incoming = s.alerts || [];
    incoming.forEach((a) => ALERTS.set(a.id, a));
    const maxId = incoming.reduce((mx, a) => Math.max(mx, a.id), lastAlertId || 0);
    if (lastAlertId === null) {                  // first load: show history, don't replay it as toasts
      lastAlertId = maxId; alertVersion = s.alert_version;
      syncToday();
      return;
    }
    const fresh = incoming.filter((a) => a.id > lastAlertId).sort((a, b) => a.id - b.id);
    lastAlertId = maxId;
    if (alertVersion !== s.alert_version) { alertVersion = s.alert_version; syncToday(); }
    fresh.forEach(notifyAlert);
    renderAlertCentre(); renderBadge();
  }

  // ---------- toasts, sound, notifications ----------
  function notifyAlert(a) {
    const rank = SEV_RANK[a.severity] ?? 3;
    if (!isSystem(a) && a.symbol && rank <= 2) RECENT.set(a.symbol, Date.now());
    const confirmedOk = !(a.kind === "breakout" && PREFS.confirmedOnly && !a.confirmed);
    if (PREFS.toast[a.kind] && confirmedOk && rank <= SEV_RANK[PREFS.toastMin]) showToast(a);
    if (PREFS.sound && audioCtx && PREFS.soundKinds[a.kind] && confirmedOk) beep(a);
    if (document.hidden) {
      unseen++;
      document.title = `(${unseen}) Alerts — Intraday Screener`;
      if ("Notification" in window && Notification.permission === "granted" && rank <= SEV_RANK[PREFS.notifyMin] && confirmedOk) {
        try {
          new Notification(`[${a.severity}] ${a.symbol || kindLabel(a)}${stratName(a) ? " · " + stratName(a) : ""}`,
            { body: (a.text || "").slice(0, 180), tag: `al-${a.id}` });
        } catch {}
      }
    }
  }
  function showToast(a) {
    const box = $("toasts");
    const el = document.createElement("div");
    el.className = `toast sev-b-${(a.severity || "").toLowerCase()}${isSystem(a) ? " sys" : ""}`;
    el.dataset.id = a.id;
    const who = a.symbol ? symLink(a.symbol) : esc(a.sector || kindLabel(a));
    el.innerHTML = `<div class="thead">${sevChip(a.severity)} <strong>${who}</strong>
        <span class="muted">${esc(stratName(a) || kindLabel(a))} · ${esc(a.time)} IST</span><button class="tclose" title="Dismiss">×</button></div>
      <div class="tmsg">${esc((a.text || "").slice(0, 280))}</div>
      <div class="tact">${a.id > 0 ? `<button class="ackbtn" data-ack="${a.id}">Acknowledge</button>` : ""}<span>${esc(DISCLAIMER)}</span></div>`;
    box.prepend(el);
    while (box.children.length > 5) box.lastElementChild.remove();
    if ((SEV_RANK[a.severity] ?? 3) >= 2) setTimeout(() => el.remove(), 12000);   // CRITICAL / HIGH stay until dismissed
  }
  $("toasts").addEventListener("click", (e) => { const x = e.target.closest(".tclose"); if (x) x.closest(".toast").remove(); });

  function tone(freq, at, dur) {
    const o = audioCtx.createOscillator(), g = audioCtx.createGain(), t0 = audioCtx.currentTime + at;
    o.frequency.value = freq; o.type = "sine";
    g.gain.setValueAtTime(0.0001, t0);
    g.gain.exponentialRampToValueAtTime(0.25, t0 + 0.02);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
    o.connect(g).connect(audioCtx.destination); o.start(t0); o.stop(t0 + dur + 0.05);
  }
  function beep(a) {
    if (!audioCtx) return;
    try {
      if (a.severity === "CRITICAL") { tone(330, 0, 0.25); tone(330, 0.32, 0.25); }
      else tone(a.direction === "down" ? 440 : 880, 0, 0.35);
    } catch {}
  }
  async function unlockAudio() {
    try { audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)(); await audioCtx.resume(); } catch {}
  }
  const renderSoundBtn = () => { $("soundToggle").textContent = PREFS.sound && audioCtx ? "🔊 Sound on" : "🔇 Sound off"; };
  $("soundToggle").addEventListener("click", async () => {
    if (PREFS.sound && audioCtx) PREFS.sound = false;
    else { await unlockAudio(); PREFS.sound = true; beep({ severity: "MEDIUM" }); }
    savePrefs(); renderSoundBtn();
  });
  $("enableAlerts").addEventListener("click", async () => {
    await unlockAudio(); PREFS.sound = true; savePrefs(); beep({ severity: "MEDIUM" });
    if ("Notification" in window && Notification.permission === "default") { try { await Notification.requestPermission(); } catch {} }
    renderSoundBtn();
    $("enableAlerts").textContent = "Alerts on"; $("enableAlerts").classList.remove("primary");
  });
  renderSoundBtn();
  document.addEventListener("visibilitychange", () => { if (!document.hidden) { unseen = 0; document.title = "Intraday Screener"; } });
  $("alertBadge").addEventListener("click", () => { $("afUnacked").checked = true; loadHistory(); $("alertCentre").scrollIntoView({ behavior: "smooth" }); });

  // ---------- acknowledgement ----------
  async function postAck(body) {
    try {
      const r = await fetch("/api/alerts/ack", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      return (await r.json()).acknowledged || 0;
    } catch { return 0; }
  }
  function markAcked(ids) {
    const when = new Date().toTimeString().slice(0, 8);
    ids.forEach((id) => {
      const a = ALERTS.get(id) || (historyRows || []).find((x) => x.id === id);
      if (a) { a.acknowledged = true; a.acknowledged_at = a.acknowledged_at || `${TODAY} ${when}`; }
      document.querySelectorAll(`.toast[data-id="${id}"]`).forEach((t) => t.remove());
    });
    renderAlertCentre(); renderBadge();
  }
  document.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-ack]");
    if (!b) return;
    b.disabled = true;
    const id = Number(b.dataset.ack);
    await postAck({ ids: [id] });
    markAcked([id]);
  });
  $("ackShown").addEventListener("click", async () => {
    const f = filters();
    const ids = alertSource(f).filter((a) => matches(a, f) && !a.acknowledged).map((a) => a.id);
    if (!ids.length) return;
    await postAck({ ids });
    markAcked(ids);
  });

  // ---------- settings ----------
  function openSettings() {
    const sevSelect = (id, val) => `<select id="${id}">${SEV.map((s) => `<option ${s === val ? "selected" : ""}>${s}</option>`).join("")}</select>`;
    const perm = "Notification" in window ? Notification.permission : "unsupported";
    $("asBody").innerHTML = `
      <p>Toast pop-ups for alerts of ${sevSelect("asToastMin", PREFS.toastMin)} or higher. Desktop notifications, while this tab is in the
        background, for ${sevSelect("asNotifyMin", PREFS.notifyMin)} or higher <span class="muted">(browser permission: ${esc(perm)}${perm === "default" ? ", use “Enable sound &amp; notifications”" : ""})</span>.</p>
      <p><label><input type="checkbox" id="asConfirmed" ${PREFS.confirmedOnly ? "checked" : ""}> Breakouts: only confirmed ones pop up, notify or sound</label></p>
      <table class="xtable"><thead><tr><th>Alert type</th><th>Toast</th><th>Sound</th><th>Severity (server)</th></tr></thead><tbody>
      ${Object.entries(KINDS).map(([k, [label, cat]]) => `<tr><td>${esc(label)} ${cat === "system" ? '<span class="tag systag">SYSTEM</span>' : ""}</td>
        <td><input type="checkbox" data-pref="toast" data-kind="${esc(k)}" ${PREFS.toast[k] ? "checked" : ""}></td>
        <td><input type="checkbox" data-pref="soundKinds" data-kind="${esc(k)}" ${PREFS.soundKinds[k] ? "checked" : ""}></td>
        <td class="muted">${esc(RULES[k] || "")}</td></tr>`).join("")}</tbody></table>
      <p class="muted">These choices are stored in this browser and only change what pops up, notifies or plays a sound: the alert centre and the database always keep every alert. Severities, scopes and cooldowns are set on the server in <code>config.ALERT_RULES</code>. Sound needs one click on “Sound” after each page load, because browsers block autoplay.</p>`;
    $("alertSettings").hidden = false;
    $("asClose").focus();
  }
  $("alertSettingsBtn").addEventListener("click", openSettings);
  $("asBody").addEventListener("change", (e) => {
    const el = e.target;
    if (el.id === "asToastMin") PREFS.toastMin = el.value;
    else if (el.id === "asNotifyMin") PREFS.notifyMin = el.value;
    else if (el.id === "asConfirmed") PREFS.confirmedOnly = el.checked;
    else if (el.dataset.pref) PREFS[el.dataset.pref][el.dataset.kind] = el.checked;
    savePrefs();
  });
  const closeSettings = () => { $("alertSettings").hidden = true; };
  $("asClose").addEventListener("click", closeSettings);
  $("alertSettings").addEventListener("click", (e) => { if (e.target === $("alertSettings")) closeSettings(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("alertSettings").hidden) closeSettings(); });

  function testAlert() {
    const sample = { id: -1, kind: "strategy_qualified", category: "setup", severity: "HIGH", symbol: "SAMPLE", time: NOW_IST || "--:--:--",
      strategy_name: "Strong Momentum", direction: "up",
      text: "Test alert: Strong Momentum all gates passed. Cumulative RVOL 2.8x; Sector RS +0.42; Stock vs sector +0.76; MACD histogram positive; price 0.35% above VWAP." };
    showToast(sample);
    if (PREFS.sound && audioCtx) beep(sample);
    if ("Notification" in window && Notification.permission === "granted") {
      try { new Notification("[HIGH] SAMPLE · Strong Momentum", { body: "Desktop notifications are working." }); } catch {}
    }
  }
  $("testAlert").addEventListener("click", () => testAlert());

  function priceBandText(c) {
    const rs = (v) => "₹" + Number(v).toLocaleString("en-IN");
    const band = c.min_price && c.max_price ? `between ${rs(c.min_price)} and ${rs(c.max_price)}`
      : c.max_price ? `at or below ${rs(c.max_price)}` : c.min_price ? `at or above ${rs(c.min_price)}` : "";
    return band ? ` Price band: only stocks whose previous close is ${band} are screened; the others are filtered and get no alerts.` : "";
  }

  function renderLegend(c) {
    $("legend").innerHTML = `
      <p><strong>Alert centre.</strong> Every alert is created on the server when something <em>changes</em> (a stock qualifies or stops qualifying, a score or RVOL crosses a threshold, RS turns positive, a MACD cross, a VWAP reclaim or loss, a breakout, a volume spike, the feed going down or stale, a data failure), never on a page refresh. It is saved in the database with the values that triggered it and a unique key, so a restart neither loses nor repeats it. Severity is an operational priority (CRITICAL feed or system problems, HIGH strong setups, MEDIUM indicator events, LOW information), not a prediction. Indicator alerts cover candidates and stocks scoring at least ${CFG.alert_watch_min ?? 50}, with a per-stock cooldown. <em>Alert settings</em> chooses what pops up as a toast, plays a sound (off until you switch it on) or sends a desktop notification while the tab is in the background; the centre always keeps everything. Filter, acknowledge, or pick a date to see older days. Alerts are informational and need your own check: they are not buy or sell instructions.</p>
      <p><strong>Strategies (tabs).</strong> After the hard filters, every stock with intraday indicators is checked against three independent strategies, each with its own gates (all thresholds in <code>config.STRATEGIES</code>): <em>Strong Momentum</em> (MOM), <em>Early Breakout</em> (BRK) and <em>Sector Leaders</em> (LDR). A stock can qualify for several. A gate whose data is missing blocks the strategy and is shown as “?”. Click any symbol to see every gate with its value.</p>
      <p><strong>Setup score (0–100).</strong> Cumulative RVOL ${c.score_weights.cum_rvol}, stock vs sector ${c.score_weights.vs_sector}, sector vs Nifty ${c.score_weights.sector_rs}, MACD ${c.score_weights.macd}, VWAP position and slope ${c.score_weights.vwap}, ADX and direction ${c.score_weights.adx}, RSI ${c.score_weights.rsi}, EMA alignment ${c.score_weights.ema}. Each part is a clamped linear scale, so no single value can dominate. Missing parts are left out and the score is points ÷ the points that could be scored (shown as “/85” when below 100). Labels: ${c.score_labels.map(([f, n]) => `${n} ≥ ${f}`).join(", ")}; with fewer than ${c.score_min_denominator} scorable points the label is withheld. <strong>It ranks setups; it is not a probability of profit.</strong></p>
      <p><strong>Candidate lifecycle.</strong> <span class="st new">NEW</span> for ${c.candidate.new_min} min after qualifying, then active; <span class="st weakening">weakening</span> when it stops qualifying; <span class="st removed">removed</span> after ${c.candidate.grace_min} more minutes without requalifying (shown greyed for ${c.candidate.removed_min} min). A change must hold ${c.candidate.confirm_s} s to count. Every change is listed under “Candidate changes” and saved in the database.</p>
      <p><strong>Bias.</strong> ${c.n_votes} directional votes (dots): price vs <em>VWAP</em>, <em>MACD</em> line vs signal, <em>+DI vs −DI</em>, <em>RS</em> (stock % change minus benchmark, ±${c.rs_min}%), price vs the <em>${c.or_minutes}-min opening range</em>${c.ema_vote ? ", the <em>5-min EMA stack</em>" : ""}${c.daily_ema_vote ? ", the <em>daily EMA stack</em>" : ""}${c.ema_vote || c.daily_ema_vote ? " (stacked ↑ bullish, stacked ↓ bearish, mixed = no vote)" : ""}. Bullish/Bearish needs at least ${c.min_votes} of ${c.n_votes} on one side, and more than the other side.</p>
      <p><strong>Score /${c.score_max}.</strong> Directional votes plus two strength checks: <em>ADX ≥ ${c.adx_min}</em> (a real trend) and <em>RVOL ≥ ${c.rvol_min}×</em> (unusual participation). Top ${c.top_n} per side are highlighted.</p>
      <p><strong>Completed candles only.</strong> MACD, RSI, DMI/ADX, EMAs, ATR and the optional metrics use finished ${c.interval}-min candles; the forming candle is ignored so values don't flicker. A “—” means the indicator doesn't have enough candles yet (hover for how many it needs).</p>
      <p><strong>MACD (${c.macd.join(", ")}) on ${c.interval}-min candles.</strong> Histogram > 0 means momentum is rising. The arrow after it is the MACD state: <span class="up">▲</span> strong bullish (line above zero and signal, histogram growing), <span class="muted">↑</span> bullish but weakening (histogram shrinking), <span class="up">↗</span> early bullish reversal (line still below zero but above its signal), <span class="down">↓</span> bearish. Hover for the line, signal and the same state on ${c.macd_context}-min candles. “× ↑/↓” marks a fresh crossover.</p>
      <p><strong>RSI (${c.rsi_period}).</strong> Wilder's Relative Strength Index on completed candles: above 50 is a positive momentum bias and 55–70 the initial preferred bullish zone; above 70 (highlighted) is potentially extended but not an automatic rejection, and ≤ 30 is also highlighted. ↗/↘ is the change over the last 3 candles. Context only, not a vote.</p>
      <p><strong>DMI · ADX (${c.dmi_period}).</strong> Shown as +DI/−DI · ADX. +DI above −DI means up-moves dominate. ADX measures how strong the trend is regardless of direction: below ${c.adx_min} (greyed) usually means choppy, ${c.adx_min}–${c.adx_strong} a developing trend, above ${c.adx_strong} a stronger one (initial heuristics to backtest). ↗/↘ shows whether ADX is rising or falling.</p>
      <p><strong>VWAP %.</strong> Distance of price from the day's volume-weighted average price; ↗/↘ is the VWAP's own slope over ${c.vwap_slope_minutes} min. “reclaim” / “lost” tags (hover for the time) mark the last time price crossed VWAP by more than ${c.vwap_buffer_pct}% and held for ${c.vwap_confirm_seconds} s. Row hover shows the EMA alignment checks, pivots/CPR, Bollinger squeeze, OBV and MFI.</p>
      <p><strong>EMA 5m ${c.ema_periods.join(" · ")}.</strong> Exponential moving averages of the ${c.interval}-min closes. Each dot is green if price is above that EMA, red if below. A green “↑” tag means the EMAs are stacked up, price &gt; EMA${c.ema_periods.join(" &gt; EMA")} (a clean uptrend), a red “↓” the reverse. “${c.ema_cross[0]}×${c.ema_cross[1]}” marks a fresh EMA${c.ema_cross[0]}/EMA${c.ema_cross[1]} crossover. ${c.ema_vote ? "The stack is one of the directional votes; the individual dots and the 9×20 tag are for context." : "Shown for context only; EMAs are not part of the score."} An EMA shows “—” until there are at least that many candles.</p>
      <p><strong>EMA day ${c.daily_ema_periods.join(" · ")}.</strong> The same idea on <em>daily</em> closes. Today's value moves with the live price, as on a daily chart: yesterday's EMA + α × (price − yesterday's EMA). Dots and ↑/↓ stack tags as above; “${c.daily_ema_cross[0]}×${c.daily_ema_cross[1]} ↑” is a golden cross and “↓” a death cross within the last ${c.daily_ema_cross_days} sessions. ${c.daily_ema_vote ? "The daily stack is also a directional vote." : "Context only, not part of the score."} Available for every stock, because daily history is loaded for all of them.</p>
      <p><strong>RVOL day</strong> (first number). Today's volume up to the last completed candle divided by the average of the previous ${c.day_rvol_sessions} sessions up to the same time of day (needs at least ${c.rvol_min_sessions} past sessions). Feeds the score; ≥ ${c.cum_rvol_threshold}× is highlighted.</p>
      <p><strong>RVOL 5m</strong> (second number). The last completed ${c.interval}-min candle's volume divided by the average volume of the <em>same time slot</em> (e.g. 10:15–10:20) over the previous ${c.day_rvol_sessions} sessions, so the busy open isn't compared with a quiet lunch hour. ≥ ${c.candle_rvol_threshold}× is highlighted. The forming candle appears in the tooltip as LIVE / INCOMPLETE and never counts as a signal.</p>
      <p><strong>Bar RVOL (⚡ alerts).</strong> Volume of the latest <em>completed</em> ${c.rvol_interval}-min candle divided by the average volume of the ${c.rvol_lookback} completed candles before it (method from the reference intraday_scanner project). Bands: LOW &lt; 0.8, NORMAL &lt; 1.2, HIGH &lt; 2.0, VERY HIGH ≥ 2.0. An alert fires when it is above <strong>${c.rvol_threshold}×</strong> on bars from ${esc(c.rvol_from)}, at most once per stock every ${c.rvol_cooldown} min. The bar's colour (green/red) gives the direction. Every NSE equity share is screened; BE/BZ (trade-for-trade) and SM/ST/SZ (SME) stocks carry a tag and get live data and spike alerts only.</p>
      <p><strong>Filters.</strong> ${c.min_turnover_cr > 0 ? `Stocks with 20-day turnover below ₹${c.min_turnover_cr} cr or ATR below ${c.min_atr_pct}% go to “Neutral &amp; filtered”: too illiquid or too quiet to trade intraday.` : `Stocks with ATR below ${c.min_atr_pct}% go to “Neutral &amp; filtered”: too quiet to trade intraday. The turnover filter is off (Settings → Min 20-day turnover), so thinly traded stocks are included: check the bid/ask before trusting a signal.`}${priceBandText(c)}</p>
      <p><strong>Breakouts.</strong> Fire once per stock per level per day when price crosses the opening-range high/low, previous-day high/low (PDH/PDL) or 52-week high/low by more than ${c.buffer_pct}%. “Confirmed” = at least ${c.min_confirmations} of MACD, DMI, ADX, RVOL and VWAP agree.</p>
      <p><strong>Sector strength.</strong> Sector RS = sector return − NIFTY 50 return, and <em>vs Sector</em> = stock return − its sector's return, each measured from the same start time on a 5-minute grid (5/15/30/60 min ago) or the previous close (day) to now. Hover for every horizon. A sector is measured by its NSE sector index where one exists, otherwise by the median of its liquid EQ-series stocks. <em>P92</em> means the stock is beating 92% of its sector peers today. “Falling less than Nifty” marks a sector that outperforms while still going down.</p>
      <p><strong>Live feed / all-NSE mode.</strong> Every stock streams price, day volume and the exchange's own VWAP over Dhan's WebSocket, and 1-min candles are built from those ticks, so ⚡ volume-spike alerts cover the whole universe (spike candle must trade at least ₹${c.rvol_min_value_lakh} lakh; Bar RVOL starts 20 candles after the feed connects). The full MACD/DMI/ADX scan runs on the most active stocks that pass the filters (by expected turnover today: the 20-day average, or today's traded value so far scaled to a full day, whichever is larger), refreshed right after each 5-min candle closes (strategy candidates and the stocks with the biggest volume bursts first); ${c.breakouts_need_filters ? "breakout pop-ups are limited to those stocks" : "breakouts fire for every stock"}. Use the search box to find any stock.</p>
      <p><strong>Tip.</strong> Hover a row for PDH/PDL, gap, 52-week range and turnover. Many breakouts fail; a confirmed one only means the indicators agree at that moment.</p>`;
  }

  // ---------- scanner tabs ----------
  let lastState = null;
  function renderScan(s) {
    const live = (c) => c.state !== "removed";
    const counts = { all: 0 };
    for (const r of s.candidates || []) {
      if (Object.values(r.cand).some(live)) counts.all++;
      for (const [k, c] of Object.entries(r.cand)) if (live(c) && META[k]) counts[META[k].short] = (counts[META[k].short] || 0) + 1;
    }
    const ordered = Object.values(META).sort((a, b) => a.order - b.order);
    const tabs = [["all", "All candidates"], ...ordered.filter((m) => m.enabled).map((m) => [m.short, m.name]),
      ["votes", "Bullish / Bearish (votes)"], ["alerts", "Alert history"], ...(s.news ? [["news", "World news"]] : [])];
    if (s.news) counts.news = s.news.count;
    if (!tabs.some(([k]) => k === TAB)) TAB = "all";
    const unread = [...ALERTS.values()].filter((a) => !a.acknowledged).length;
    const tabHtml = tabs.map(([k, name]) => `<button role="tab" data-tab="${k}" aria-selected="${k === TAB}">${esc(name)}${k === "votes" ? "" : `<span class="n">${k === "alerts" ? unread : counts[k] || 0}</span>`}</button>`).join("");
    if ($("tabs")._html !== tabHtml) { $("tabs").innerHTML = tabHtml; $("tabs")._html = tabHtml; }
    const votes = TAB === "votes", alertsTab = TAB === "alerts", newsTab = TAB === "news";
    $("votesGrid").hidden = !votes; $("candWrap").hidden = votes || alertsTab || newsTab;
    $("candLogPanel").hidden = votes || alertsTab || newsTab;
    $("rowFilters").hidden = newsTab;                     // stock filters mean nothing for news
    placeAlertCentre(alertsTab);
    if (window.NewsTab) {
      if (newsTab) { window.NewsTab.show($("newsTab")); window.NewsTab.updated(s.news && s.news.updated); } else window.NewsTab.hide();
    }
    if (newsTab) { $("tabNote").textContent = "Economic news from public feeds, updated on a schedule. Headlines open on the publisher's site."; return; }
    if (alertsTab) { $("tabNote").textContent = "Every alert of the day (or pick a date). The same alert centre is also below the tables."; return; }
    if (votes) { $("tabNote").textContent = "The original view: bullish/bearish by directional votes (score out of " + SCORE_MAX + ")."; return; }
    const key = keyOf(TAB);
    $("tabNote").innerHTML = TAB === "all"
      ? `Stocks that qualify for at least one strategy, ranked by <strong>setup score</strong> (0–100, a scanner score, not a probability of profit); scores resting on too little data (“data?”) rank after the rest. Hard filters run first. Click a symbol to see every gate.`
      : `<strong>${esc(META[key].name)}</strong> needs all of: ${META[key].gates.map(([, txt]) => esc(txt)).join(" · ")}.`;
    const rows = (s.candidates || []).filter((r) => (TAB === "all" || r.cand[key]) && rowFilter(r));
    const thin = (r) => r.score_label === "insufficient data" || r.setup_score === null || r.setup_score === undefined;
    rows.sort((a, b) => (STATE_ORDER[lifeOf(a).state] >= 2) - (STATE_ORDER[lifeOf(b).state] >= 2)   // weakening/removed last
      || thin(a) - thin(b)                                                                        // then scores on < 70 points
      || (b.setup_score ?? -1) - (a.setup_score ?? -1));
    let n = 0;
    rows.forEach((r) => { r._rank = STATE_ORDER[lifeOf(r).state] >= 2 ? "" : ++n; });
    renderTable($("candTable"), rows, CAND_COLS, s.ready ? (TAB === "all" ? "No stock qualifies for any strategy right now." : `No stock qualifies for ${esc(META[key].name)} right now.`) : "Loading…",
      (r) => { const c = lifeOf(r); return c ? (c.state === "new" ? "fresh" : c.state) : ""; });
    const ev = s.candidate_events || [];
    $("candLogCount").textContent = ev.length ? `(${ev.length})` : "";
    const EV = { qualified: ["up", "qualified"], requalified: ["up", "qualifies again"], weakening: ["warn", "stopped qualifying"], removed: ["muted", "removed"] };
    $("candLog").innerHTML = ev.length ? ev.map((e) => {
      const [cls, txt] = EV[e.event] || ["", e.event];
      const m = META[e.strategy] || { short: e.strategy };
      return `<li><span class="muted">${esc(e.time)}</span><span class="stag ${e.event === "removed" ? "removed" : e.event === "weakening" ? "weakening" : "qualified"}">${esc(m.short)}</span>${symLink(e.symbol)}
        <span class="${cls === "warn" ? "" : cls}">${txt}</span><span class="muted">score ${e.score ?? "—"} · ₹${num(e.price)}${e.reasons && e.reasons.length ? " · " + esc(e.reasons.map((c) => gateName(e.strategy, c)).join("; ")) : ""}</span></li>`;
    }).join("") : '<li class="muted">No changes yet.</li>';
  }
  $("tabs").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-tab]");
    if (!b) return;
    TAB = b.dataset.tab; store.set("tab", TAB);
    if (lastState) renderScan(lastState);
  });
  const alertHome = document.createComment("alert centre home");
  $("alertCentre").before(alertHome);
  function placeAlertCentre(inTab) {
    const el = $("alertCentre");
    if (inTab && el.parentElement !== $("alertTabSlot")) { $("alertTabSlot").appendChild(el); el.classList.remove("panel"); }
    if (!inTab && el.parentElement === $("alertTabSlot")) { alertHome.after(el); el.classList.add("panel"); }
  }

  // ---------- filters (sector, strategy, score, RVOL, direction) ----------
  let FILT = {};
  try { FILT = JSON.parse(store.get("rowFilters") || "{}") || {}; } catch {}
  function rowFilter(r) {
    if (FILT.sector && r.sector !== FILT.sector) return false;
    if (FILT.strategy) {
      const k = keyOf(FILT.strategy), v = (r.strat || {})[FILT.strategy];
      if (!((r.cand && r.cand[k]) || (v && v[0] === "qualified"))) return false;
    }
    if (FILT.score !== undefined && FILT.score !== "" && !((r.setup_score ?? -1) >= +FILT.score)) return false;
    if (FILT.rvol !== undefined && FILT.rvol !== "" && !((r.rvol ?? -1) >= +FILT.rvol)) return false;
    if (FILT.dir && r.bias !== FILT.dir) return false;
    const [lo, hi] = chgBand();                       // the server already applied it to the vote tables
    if ((lo !== null || hi !== null) && !(r.chg_pct !== null && r.chg_pct !== undefined
        && (lo === null || r.chg_pct >= lo) && (hi === null || r.chg_pct <= hi))) return false;
    return true;
  }
  const chgBand = () => (CFG && CFG.chg_band) || [null, null];
  const bandOn = () => chgBand().some((v) => v !== null);
  const filtersOn = () => !!(FILT.sector || FILT.strategy || (FILT.score ?? "") !== "" || (FILT.rvol ?? "") !== "" || FILT.dir || bandOn());
  function fillFilterOptions(s) {
    const secs = [...new Set((s.sectors || []).map((t) => t.sector))].sort();
    const sig = secs.join("|");
    if ($("fSector")._sig !== sig) {
      $("fSector").innerHTML = '<option value="">All sectors</option>' + secs.map((x) => `<option>${esc(x)}</option>`).join("");
      $("fSector")._sig = sig;
    }
    if ($("fStrategy").options.length <= 1 && Object.keys(META).length) {
      $("fStrategy").innerHTML = '<option value="">Any strategy</option>' + Object.values(META).sort((a, b) => a.order - b.order)
        .map((m) => `<option value="${esc(m.short)}">${esc(m.name)}</option>`).join("");
    }
    $("fSector").value = FILT.sector || ""; $("fStrategy").value = FILT.strategy || "";
    if (document.activeElement !== $("fScore")) $("fScore").value = FILT.score ?? "";
    if (document.activeElement !== $("fRvol")) $("fRvol").value = FILT.rvol ?? "";
    $("fDir").value = FILT.dir || "";
    const [lo, hi] = chgBand();
    if (!chgDirty) { $("fChgMin").value = lo ?? ""; $("fChgMax").value = hi ?? ""; }
  }
  // Change % band: a server-side setting (TABLE_CHG_PCT_MIN / MAX), so it reaches every stock, not only
  // the rows that were sent. Saved through the settings API, like the settings page does.
  // chgDirty: the boxes hold the user's typing (not yet saved, or rejected), so redraws mustn't reset them.
  let chgTimer = null, chgDirty = false, chgError = "";
  async function saveChgBand(lo, hi) {
    const num = (v) => (v === "" || v === null ? null : Number(v));
    try {
      const res = await fetch("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ changes: { TABLE_CHG_PCT_MIN: num(lo), TABLE_CHG_PCT_MAX: num(hi) } }) });
      const r = await res.json();
      chgError = res.ok ? "" : Object.values(r.errors || {}).join("; ") || r.error || `HTTP ${res.status}`;
      if (!chgError && CFG) CFG.chg_band = [num(lo), num(hi)];    // until the next push confirms it
    } catch (e) { chgError = String(e); }
    if (!chgError && $("fChgMin").value === String(lo ?? "") && $("fChgMax").value === String(hi ?? "")) chgDirty = false;
    rerender();
  }
  const readChg = () => {
    chgDirty = true;
    clearTimeout(chgTimer);
    chgTimer = setTimeout(() => saveChgBand($("fChgMin").value, $("fChgMax").value), 600);
  };
  ["fChgMin", "fChgMax"].forEach((id) => $(id).addEventListener("input", readChg));
  const readFilters = () => {
    FILT = { sector: $("fSector").value, strategy: $("fStrategy").value, score: $("fScore").value, rvol: $("fRvol").value, dir: $("fDir").value };
    store.set("rowFilters", JSON.stringify(FILT));
    rerender();
  };
  ["fSector", "fStrategy", "fDir"].forEach((id) => $(id).addEventListener("change", readFilters));
  ["fScore", "fRvol"].forEach((id) => $(id).addEventListener("input", readFilters));
  $("fReset").addEventListener("click", () => {
    FILT = {}; store.set("rowFilters", "{}");
    clearTimeout(chgTimer); chgDirty = false; chgError = "";
    $("fChgMin").value = $("fChgMax").value = "";
    if (bandOn()) saveChgBand("", "");
    fillFilterOptions(lastState || {}); rerender();
  });

  // ---------- "why?" view ----------
  const GATE_ICON = { pass: ["ok", "✓"], fail: ["no", "✗"], na: ["na", "?"] };
  function explainHtml(d) {
    const filt = d.passes_filters ? '<span class="up">✓ passes the hard filters</span>' : `<span class="down">✗ hard filters: ${esc((d.filter_reasons || []).join("; "))}</span>`;
    const parts = (d.score_detail || []).map((p) => `<tr><td>${esc(p.label)} <span class="muted">(weight ${p.weight})</span></td><td>${p.available ? `${num(p.points, 1)} / ${num(p.max, 1)}` : '<span class="muted">n/a</span>'}</td><td class="muted">${esc(p.detail)}</td></tr>`).join("");
    const score = d.setup_score === null || d.setup_score === undefined ? '<p class="muted">Not scored (hard filter or no intraday indicators).</p>'
      : `<p><strong>Setup score ${d.setup_score}</strong> (${esc(d.score_label)}) = ${num(d.score_points, 1)} points of ${num(d.score_denominator, 1)} available × 100. <span class="muted">A scanner score, not a probability of profit.</span></p><table class="xtable">${parts}</table>`;
    const strategies = (d.strategies || []).map((r) => {
      const life = (d.candidates || {})[r.key];
      const head = `<strong>${esc(r.name)}</strong> <span class="stag ${r.qualified ? "qualified" : "off"}">${esc(r.short)}</span> ${esc(r.summary)}${life ? ` · candidate <span class="st ${life.state}">${esc(life.state)}</span> since ${esc(life.since)}` : ""}`;
      const gates = (r.gates || []).map((g) => { const [cls, icon] = GATE_ICON[g.status] || ["", "·"]; return `<li><span class="${cls}">${icon}</span> <strong>${esc(g.code)}</strong> ${esc(g.need)} <span class="muted">— ${esc(g.text)}</span></li>`; }).join("");
      return `<div>${head}<ul class="gates">${gates}</ul></div>`;
    }).join("");
    const evs = (d.events || []).map((e) => `<li>${esc(e.time)} ${esc((META[e.strategy] || {}).short || e.strategy)} ${esc(e.event)} · score ${e.score ?? "—"}</li>`).join("");
    return `<p>${filt}</p>${score}${strategies}${evs ? `<p><strong>Candidate lifecycle today</strong></p><ul class="gates">${evs}</ul>` : ""}
      <p class="muted">✓ pass · ✗ fail · ? data unavailable (blocks the strategy unless the gate is in skip_if_missing). Change the thresholds on the Settings page (config ${esc(d.config_id)}).</p>`;
  }
  document.addEventListener("click", (e) => {
    const a = e.target.closest("a.sym");
    if (!a) return;
    e.preventDefault(); e.stopPropagation();
    openDetail(a.dataset.sym);
  });

  // ---------- stock detail drawer ----------
  let DETAIL = { sym: null, interval: Number(store.get("chartInterval")) || 5, lastBar: null, data: null, chart: null, at: 0 };
  const kv = (pairs) => `<div class="kv">${pairs.filter(([, v]) => v !== undefined).map(([k, v]) => `<div><span>${esc(k)}</span>${v}</div>`).join("")}</div>`;
  const pctRow = (label, o) => `<tr><td>${esc(label)}</td>${["5m", "15m", "30m", "60m", "day"].map((h) => `<td>${o && o[h] !== null && o[h] !== undefined ? signed(o[h], 2) : '<span class="muted">—</span>'}</td>`).join("")}</tr>`;
  function detailHtml(d) {
    const rv = d.rvol || {}, dd = rv.day_detail || {}, cd = rv.candle_detail || {}, lv = d.levels || {}, iv = d.indicators || {}, q = d.quality || {};
    const ret = d.returns || {};
    const alerts = (d.alerts || []).map((a) => `<li>${esc(a.time)} ${sevChip(a.severity)} ${esc(kindLabel(a))}${a.strategy ? ` · ${esc(stratName(a))}` : ""} <span class="muted">— ${esc(a.text || "")}</span></li>`).join("");
    return `
      <div class="ivbtns" role="group" aria-label="Candle size">${[1, 5, 15].map((m) => `<button data-iv="${m}" aria-pressed="${m === DETAIL.interval}">${m}m</button>`).join("")}</div>
      <span class="muted" id="chartNote"></span>
      <div class="chart-legend"><span><i style="background:#d97706"></i>VWAP</span><span><i style="background:#2563eb"></i>EMA 9</span><span><i style="background:#7c3aed"></i>EMA 20</span>
        <span><i style="background:#0891b2"></i>opening range</span><span><i style="background:var(--muted)"></i>PDH / PDL</span><span>faded candle = still forming</span></div>
      <div id="chartBox"><p class="muted">Loading chart…</p></div>
      <div class="readout" id="readout"></div>
      <h3>Quote</h3>
      ${kv([["LTP", num(d.ltp)], ["Change", signed(d.chg_pct, 2, "%")], ["Open", num(d.open)], ["Day high", num(d.day_high)], ["Day low", num(d.day_low)],
        ["Gap", signed(d.gap_pct, 2, "%")], ["Sector", esc(d.sector || "—")], ["Industry", esc(d.industry || "—")], ["Series", esc(d.series || "—")],
        ["Vote bias", `${esc(d.bias || "—")} (${d.vote_score ?? "—"}/${SCORE_MAX})`]])}
      <h3>Strategies and setup score</h3>
      ${explainHtml(d)}
      <h3>Relative strength: stock, sector, NIFTY 50</h3>
      <table class="xtable"><thead><tr><th></th><th>5m</th><th>15m</th><th>30m</th><th>60m</th><th>day</th></tr></thead><tbody>
        ${pctRow("Stock return %", ret.stock)}${pctRow(`Sector return % (${d.sector || "—"})`, ret.sector)}${pctRow("NIFTY 50 return %", ret.nifty)}
        ${pctRow("Sector vs Nifty", ret.sector_rs)}${pctRow("Stock vs sector", ret.vs_sector)}</tbody></table>
      <p class="muted">Percentile in its sector: ${ret.sector_pctile ?? "—"} of ${ret.sector_peer_count ?? 0} peers.</p>
      <h3>Volume (RVOL) and its baseline</h3>
      ${kv([["Day RVOL", rv.day !== null && rv.day !== undefined ? `${num(rv.day, 2)}×` : "—"], ["Today to " + (dd.through || "—"), num(dd.today, 0)],
        ["Usual to the same time", num(dd.baseline, 0)], ["Sessions in baseline", `${dd.sessions ?? "—"} (min ${dd.min_sessions ?? "—"})`],
        ["5m RVOL", rv.candle !== null && rv.candle !== undefined ? `${num(rv.candle, 2)}× (${esc(cd.bar || "")})` : "—"],
        ["That candle / usual", `${num(cd.volume, 0)} / ${num(cd.baseline, 0)}`], ["Forming candle", cd.live_rvol !== null && cd.live_rvol !== undefined ? `${num(cd.live_rvol, 2)}× LIVE` : "—"],
        ["Bar RVOL (1-min)", rv.bar !== null && rv.bar !== undefined ? `${num(rv.bar, 2)}× (${esc((rv.bar_time || "").slice(11, 16))})` : "—"]])}
      <h3>Indicators (completed candles)</h3>
      ${kv([["MACD", `${num(iv.macd, 3)} / ${num(iv.macd_signal, 3)} · hist ${num(iv.macd_hist, 3)}`], ["MACD state", esc((iv.macd_state || "—").replace(/_/g, " "))],
        ["15-min MACD", esc((iv.macd15_state || "—").replace(/_/g, " "))], ["RSI", `${num(iv.rsi, 1)} (${iv.rsi_slope > 0 ? "↗" : iv.rsi_slope < 0 ? "↘" : "→"})`],
        ["ADX · +DI · −DI", `${num(iv.adx, 1)} · ${num(iv.plus_di, 1)} · ${num(iv.minus_di, 1)} ${iv.adx_rising ? "↗" : iv.adx_rising === false ? "↘" : ""}`],
        ["EMA 9 / 20 / 50 / 200", `${num(iv.ema9)} / ${num(iv.ema20)} / ${num(iv.ema50)} / ${num(iv.ema200)}`], ["VWAP", `${num(iv.vwap)} (${signed(iv.vwap_dist_pct, 2, "%")})`],
        ["VWAP slope", iv.vwap_slope_pct !== null && iv.vwap_slope_pct !== undefined ? signed(iv.vwap_slope_pct, 3, "%") : "—"], ["ATR daily / 5-min", `${num(iv.atr_pct, 2)}% / ${num(iv.atr_intraday_pct, 2)}%`],
        ["Bollinger width", `${num(iv.bb_bandwidth, 2)}%${iv.bb_squeeze ? " · SQUEEZE" : ""}`], ["OBV · MFI", `${esc(iv.obv_trend || "—")} · ${num(iv.mfi, 0)}`]])}
      <h3>Levels</h3>
      ${kv([["Opening range", lv.or_complete ? `${num(lv.or_low)} – ${num(lv.or_high)}` : "forming"], ["PDH / PDL", `${num(lv.pdh)} / ${num(lv.pdl)}`],
        ["Prev close", num(lv.pdc)], ["52-week", `${num(lv.w52_low)} – ${num(lv.w52_high)}`], ["Pivot · CPR", `${num(lv.pivot)} · ${num(lv.cpr_bc)}–${num(lv.cpr_tc)}`],
        ["R1 / S1", `${num(lv.r1)} / ${num(lv.s1)}`], ["R2 / S2", `${num(lv.r2)} / ${num(lv.s2)}`],
        ["Fresh break", d.breakout_level ? `${esc(d.breakout_level.name)} ${num(d.breakout_level.level)} (${esc(d.breakout_level.bar)})` : "—"]])}
      <h3>Alerts today</h3>
      ${alerts ? `<ul class="gates">${alerts}</ul>` : '<p class="muted">None for this stock.</p>'}
      <h3>Data quality</h3>
      ${kv([["Data state", esc(q.data_state || "—")], ["Last candle", esc(q.last_bar || "—")], ["Completed candles", num(q.completed_bars, 0)],
        ["Daily candles", num(q.daily_bars, 0)], ["RVOL sessions", q.rvol_sessions ?? "—"], ["Last price update", esc(q.last_ltp_update || "—")],
        ["Last trade (feed)", esc(q.last_trade || "—")], ["Full indicator scan", q.deep_scan === false ? "no (outside the deep scan)" : "yes"],
        ["Unavailable", q.unavailable && q.unavailable.length ? esc(q.unavailable.join(", ")) : "none"]])}
      <p class="muted">${esc(DISCLAIMER || "For learning only.")}</p>`;
  }
  async function loadChart(force) {
    if (!DETAIL.sym) return;
    const sym = DETAIL.sym, ivl = DETAIL.interval;
    try {
      const res = await fetch(`/api/chart/${encodeURIComponent(sym)}?interval=${ivl}`, { cache: "no-store" });
      const data = await res.json();
      if (!res.ok || sym !== DETAIL.sym || ivl !== DETAIL.interval) return;
      DETAIL.chart = data;
      const box = $("chartBox");
      if (box) {
        Charts.draw(box, data, $("readout"));
        $("chartNote").textContent = `${data.sessions.length} session${data.sessions.length === 1 ? "" : "s"} · updated ${data.generated} · ${data.vwap_note}`;
      }
    } catch (err) { const box = $("chartBox"); if (box) box.innerHTML = '<p class="muted">Chart unavailable.</p>'; }
  }
  async function loadDetail() {
    const sym = DETAIL.sym;
    if (!sym) return;
    DETAIL.at = Date.now();
    let d;
    try {
      const res = await fetch(`/api/stock/${encodeURIComponent(sym)}`, { cache: "no-store" });
      d = await res.json();
      if (!res.ok) throw new Error(d.error || res.statusText);
    } catch (err) {
      $("detailBody").innerHTML = `<p class="muted">${esc(err.message || "Could not load this stock.")}</p>`;
      return;
    }
    if (sym !== DETAIL.sym) return;
    DETAIL.data = d;
    const scroll = $("detailBody").scrollTop;
    $("detailTitle").textContent = d.symbol;
    $("detailQuote").innerHTML = `${num(d.ltp)} ${signed(d.chg_pct, 2, "%")} <span class="muted">${esc(d.company || "")} · ${esc(d.time)} IST</span>`;
    $("detailBody").innerHTML = detailHtml(d);
    $("detailBody").scrollTop = scroll;
    if (DETAIL.chart && DETAIL.chart.symbol === sym && DETAIL.chart.interval === DETAIL.interval) Charts.draw($("chartBox"), DETAIL.chart, $("readout"));
  }
  function openDetail(sym) {
    sym = String(sym || "").toUpperCase();
    if (!sym) return;
    const changed = sym !== DETAIL.sym;
    DETAIL.sym = sym; SELECTED = sym;
    if (changed) { DETAIL.chart = null; DETAIL.lastBar = null; $("detailBody").innerHTML = '<p class="muted">Loading…</p>'; $("detailTitle").textContent = sym; $("detailQuote").textContent = ""; }
    $("detail").hidden = false;
    history.replaceState(null, "", `#stock=${encodeURIComponent(sym)}`);
    loadDetail().then(() => loadChart(true));
    rerender();
  }
  function closeDetail() {
    $("detail").hidden = true; DETAIL.sym = null; SELECTED = null;
    if (/^#(stock|why)=/.test(location.hash)) history.replaceState(null, "", location.pathname);
    rerender();
  }
  function refreshDetailIfNeeded(s) {
    if (!DETAIL.sym || $("detail").hidden) return;
    const row = STATE.rows[DETAIL.sym] || [...(s.bullish || []), ...(s.bearish || []), ...(s.neutral || [])].find((r) => r.symbol === DETAIL.sym);
    const bar = row ? row.last_bar : null;
    if (bar && bar !== DETAIL.lastBar) { DETAIL.lastBar = bar; loadChart(); }            // a new candle: redraw the chart
    else if (DETAIL.interval === 1 && Date.now() - DETAIL.at > 60000) loadChart();
    if (Date.now() - DETAIL.at > 10000) loadDetail();                                        // quote, gates, alerts
  }
  $("detailClose").addEventListener("click", closeDetail);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("detail").hidden && $("alertSettings").hidden) closeDetail(); });
  $("detailBody").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-iv]");
    if (!b) return;
    DETAIL.interval = Number(b.dataset.iv); store.set("chartInterval", String(DETAIL.interval));
    $("detailBody").querySelectorAll("button[data-iv]").forEach((x) => x.setAttribute("aria-pressed", String(Number(x.dataset.iv) === DETAIL.interval)));
    $("chartBox").innerHTML = '<p class="muted">Loading chart…</p>';
    loadChart(true);
  });
  window.addEventListener("resize", () => { if (DETAIL.chart && !$("detail").hidden && $("chartBox")) Charts.draw($("chartBox"), DETAIL.chart, $("readout")); });

  // ---------- search ----------
  const SEARCH_COLS = [["Bias", (r) => `<span class="tag bias-${r.bias}">${esc(r.bias)}</span>`],
    ["Setup", (r) => setupCell(r)], ["Strategies", (r) => stratTags(r)], ...NEUTRAL_COLS];
  let searchTimer = null, searchSeq = 0;
  let lastSearchAt = 0;
  function maybeRefreshSearch() { if (Date.now() - lastSearchAt > 3000) refreshSearch(); }
  async function refreshSearch() {
    lastSearchAt = Date.now();
    const q = $("search").value.trim();
    if (!q) { $("searchWrap").hidden = true; return; }
    const seq = ++searchSeq;
    try {
      const rows = await (await fetch(`/api/search?q=${encodeURIComponent(q)}`, { cache: "no-store" })).json();
      if (seq !== searchSeq) return;
      $("searchWrap").hidden = false;
      renderTable($("searchTable"), rows, SEARCH_COLS, `No stock matching “${esc(q)}” in the scan yet.`);
    } catch {}
  }
  $("search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(refreshSearch, 250); });

  // ---------- main loop ----------
  let legendDone = false;
  function renderToken(tk) {
    const el = $("token");
    el.hidden = !tk;
    if (!tk) return;
    const h = tk.hours_left;
    el.className = `badge ${{ expiring: "warn", unknown: "warn", failing: "err", expired: "err" }[tk.state] || ""}`;
    el.textContent = tk.state === "expired" ? "token expired" : h === null ? "token ?" : `token ${h < 1 ? `${Math.max(0, Math.round(h * 60))} min` : `${Math.floor(h)} h`} left`;
    const plan = tk.auto_renew
      ? `Auto-renewal at ${tk.times.length ? tk.times.join(", ") : "no set times"} IST${tk.on_exit ? " and when the app stops" : ""}; next ${tk.next ? `${tk.next.label} (${tk.next.why})` : "—"}.`
      : "Auto-renewal is off (Settings → Access token).";
    const last = tk.last ? ` Last: ${tk.last.ok ? "OK" : "failed"} at ${tk.last.time.slice(11, 16)} (${tk.last.reason}): ${tk.last.message}.` : "";
    el.title = `Dhan access token valid until ${tk.expires_label || "unknown"} IST. ${plan}${last}`;
  }

  function renderAll(s) {
    if (s.config) CFG = s.config;
    if (s.strategy_meta) META = s.strategy_meta;
    lastState = s;
    if (s.config && s.config.score_max) SCORE_MAX = s.config.score_max;       // before any table is drawn
    if (s.config && s.config.ema_periods) EMA_PERIODS = s.config.ema_periods;
    if (s.config && s.config.daily_ema_periods) { DAILY_EMA_PERIODS = s.config.daily_ema_periods; DAILY_CROSS = s.config.daily_ema_cross; }
    const demo = /DEMO/.test(s.source);
    $("source").textContent = s.source; $("source").className = `badge ${demo ? "demo" : "live"}`;
    $("phase").textContent = s.market_phase === "holiday" ? `NSE holiday${s.holiday ? `: ${s.holiday}` : ""} · ${s.now.slice(11, 19)} IST`
      : `market ${s.market_phase} · ${s.now.slice(11, 19)} IST`;
    $("phase").className = `badge ${s.market_phase === "holiday" ? "warn" : ""}`;
    const badState = { stale: "DATA STALE", disconnected: "FEED DISCONNECTED", stopped: "FEED STOPPED" }[s.data_state]
      || (s.history && s.history.stalled_since ? "INDICATORS BEHIND" : "");
    $("dataState").hidden = !badState; $("dataState").textContent = badState || "";
    const br = s.breadth || {};
    if (br.advancing && br.advancing.of) {
      const pct = (o) => o && o.of ? `${Math.round(100 * o.n / o.of)}%` : "n/a";
      $("breadth").hidden = false;
      $("breadth").innerHTML = `breadth <span class="up">▲ ${num(br.advancing.n, 0)}</span> <span class="down">▼ ${num(br.declining.n, 0)}</span> · above VWAP ${pct(br.above_vwap)}`;
      $("breadth").title = `Above EMA20 (5-min): ${br.above_ema20.n}/${br.above_ema20.of} · at day high: ${br.at_day_high.n} · at day low: ${br.at_day_low.n} · Day RVOL > ${br.rvol_threshold}: ${br.rvol_above.n}/${br.rvol_above.of}`;
    }
    renderSectors(s.sectors);
    $("sectorCount").textContent = s.sectors && s.sectors.length ? `(${s.sectors.length})` : "";
    const b = s.benchmark;
    $("bench").innerHTML = b.chg_pct === null ? "benchmark —"
      : `${esc(b.source === b.name ? b.name : "Watchlist median")}${b.source === b.name && b.ltp ? ` ${num(b.ltp)}` : ""} ${signed(b.chg_pct, 2, "%")}`;
    const live = s.mode === "live";
    const f = s.feed, h = s.history;
    $("updated").textContent = `prices ${s.last_ltp_update || "—"} · candles ${s.last_candle_update || "—"} · ${num(s.symbols_resolved, 0)}/${num(s.symbols_total, 0)} symbols · history: ${s.storage || "off"}`;
    $("feed").hidden = !live;
    if (live) {
      const state = f.fatal ? "stopped" : f.connected ? "live" : "reconnecting";
      $("feed").className = `badge ${f.fatal ? "err" : f.connected ? "live" : "warn"}`;
      $("feed").textContent = `Dhan feed ${state} · ${num(f.with_data, 0)}/${num(f.instruments, 0)} streaming${f.packets_per_sec !== null ? ` · ${num(f.packets_per_sec, 0)} pkt/s` : ""}${f.last_packet ? ` · last tick ${f.last_packet}` : ""}`;
      $("universeInfo").textContent = `${s.universe === "nse" ? "All NSE equity shares" : "Watchlist"}: ${num(s.counts.rows, 0)} with live data · daily history ${num(h.daily_loaded, 0)}/${num(h.daily_total, 0)} · full indicator scan on ${num(h.deep_scanned, 0)} most active stocks (max ${num(h.deep_max, 0)}, by expected turnover today)${h.last_fetch ? ` · last refresh ${h.last_fetch}` : ""}`;
    } else {
      $("universeInfo").textContent = `Watchlist: ${num(s.counts.rows, 0)} stocks`;
    }
    renderToken(s.token);

    let banner = "", cls = "banner";
    const tk = s.token;
    if (s.auth_error) { banner = `Dhan rejected the credentials: ${s.auth_error}. Generate a new access token (web.dhan.co → My Profile → Access DhanHQ APIs) and put it in .env${tk ? ": the running app loads it within a minute" : ", then restart"}.`; cls += " error"; }
    else if (tk && tk.state === "expired") { banner = `The Dhan access token expired (${tk.expires_label} IST) and can't be renewed. Generate a new one on web.dhan.co and put it in .env: the running app loads it within a minute.`; cls += " error"; }
    else if (tk && tk.state === "failing") { banner = `Access-token renewal failed: ${tk.last ? tk.last.message : "unknown error"}. Retrying every few minutes; the current token is valid until ${tk.expires_label} IST.`; cls += " error"; }
    else if (h && h.stalled_since) { banner = `The 5-min indicator refresh has made no progress since ${h.stalled_since}: the strategy tabs, scores and breakouts are behind the market. Usually the network or Dhan; it resumes by itself.`; cls += " error"; }
    else if (s.pause) { banner = `The computer was asleep (or the app was frozen) from ${s.pause.from} to ${s.pause.to}: prices, candles and alerts paused and are catching up, so anything from that window may show up late. Keep the Mac awake and the lid open during market hours.`; cls += " error"; }
    else if (s.data_state === "stale") { banner = `No price updates for over ${s.config.stale_seconds} seconds while the market is open: prices on screen may be out of date.`; cls += " error"; }
    else if (live && !f.connected) banner = `Live feed disconnected${f.last_error ? ` (${f.last_error})` : ""} — reconnecting automatically…`;
    else if (!s.ready) banner = live ? "Connecting to the live feed…" : "Loading candles and computing indicators… (first scan takes ~30 s for 50 stocks)";
    else if (live && h.daily_loaded < h.daily_total) banner = `Loading daily history ${num(h.daily_loaded, 0)}/${num(h.daily_total, 0)} (first run ≈ ${Math.ceil((h.daily_total - h.daily_loaded) / 240)} min, cached afterwards). Stocks appear in the bullish/bearish lists as their history and indicators load.`;
    else if (s.market_phase === "holiday") banner = `NSE is closed today${s.holiday ? ` (${s.holiday})` : ""}. The tables show the last session; nothing updates until the next trading day.`;
    else if (demo) banner = "Demo mode: prices are simulated random walks on a fast clock, not real market data.";
    else if (s.calendar && s.calendar.warning) banner = s.calendar.warning + ".";
    $("banner").hidden = !banner; $("banner").className = cls; $("banner").textContent = banner;

    fillFilterOptions(s);
    const bull = s.bullish.filter(rowFilter), bear = s.bearish.filter(rowFilter), neu = s.neutral.filter(rowFilter);
    const shown = (n, total) => total > n ? `(top ${num(n, 0)} of ${num(total, 0)})` : `(${num(total, 0)})`;
    $("bullCount").textContent = shown(bull.length, filtersOn() ? bull.length : s.counts.bullish);
    $("bearCount").textContent = shown(bear.length, filtersOn() ? bear.length : s.counts.bearish);
    $("neutralCount").textContent = shown(neu.length, filtersOn() ? neu.length : s.counts.neutral);
    const [blo, bhi] = chgBand();
    const bandText = bandOn() ? ` · change % ${blo ?? "any"} to ${bhi ?? "any"} checked across all ${num(s.counts.rows, 0)} stocks` : "";
    $("fInfo").textContent = chgError ? `Chg %: ${chgError}` : filtersOn() ? `filters on: they apply to the candidate and vote tables${bandText}` : "";
    $("fInfo").className = chgError ? "down" : "muted";
    maybeRefreshSearch();
    renderTable($("bullTable"), bull, COLS, s.ready ? (filtersOn() ? "No bullish stock matches the filters." : "No stock meets the bullish rules right now.") : "Loading…");
    renderTable($("bearTable"), bear, COLS, s.ready ? (filtersOn() ? "No bearish stock matches the filters." : "No stock meets the bearish rules right now.") : "Loading…");
    renderTable($("neutralTable"), neu, NEUTRAL_COLS, "None.");
    renderScan(s);
    if (!legendDone && s.config) { renderLegend(s.config); legendDone = true; }
    refreshDetailIfNeeded(s);

    $("errorsPanel").hidden = !s.errors.length;
    $("errorCount").textContent = `(${s.errors.length})`;
    $("errorList").innerHTML = s.errors.map((e) => `<li>${esc(e.time)} ${esc(e.message)}${e.count > 1 ? ` ×${e.count}` : ""}</li>`).join("");
  }
  const rerender = () => { if (lastState) renderAll(lastState); };
  function handleSnapshot(s) {
    renderAll(s);
    ingestAlerts(s);
    if (!booted) {
      booted = true;
      if (location.hash === "#test-alert") testAlert();
      if (location.hash === "#alert-settings") openSettings();
      const m = location.hash.match(/^#(?:why|stock)=(.+)$/);
      if (m) openDetail(decodeURIComponent(m[1]));
    }
  }
  let booted = false;

  // ---------- updates: WebSocket push (incremental), polling as the fallback ----------
  const STATE = { rows: {}, lists: {}, cand: {}, meta: null };
  let ws = null, wsRetry = 0, pollTimer = null, pushMode = "connecting";
  function renderPush() {
    const t = { live: ["live push", "push-live", "WebSocket: the server sends only what changed"],
      polling: ["polling", "push-poll", "WebSocket unavailable: polling /api/state every 3 s"],
      reconnecting: ["reconnecting…", "push-poll", "WebSocket closed: reconnecting (polling meanwhile)"],
      connecting: ["connecting…", "", ""] }[pushMode];
    $("push").textContent = t[0]; $("push").className = `badge ${t[1]}`; $("push").title = t[2];
  }
  function snapshotFromState(alerts) {
    const list = (name) => (STATE.lists[name] || []).map((sym) => STATE.rows[sym]).filter(Boolean);
    return { ...STATE.meta, bullish: list("bullish"), bearish: list("bearish"), neutral: list("neutral"),
      candidates: (STATE.lists.candidates || []).filter((sym) => STATE.rows[sym])
        .map((sym) => ({ ...STATE.rows[sym], cand: STATE.cand[sym] || {} })), alerts };
  }
  function applyMessage(m) {
    if (m.type === "full") {
      STATE.rows = m.rows || {}; STATE.lists = m.lists || {}; STATE.cand = m.cand || {}; STATE.meta = m.meta || {};
    } else {
      for (const [sym, ch] of Object.entries(m.rows || {})) {
        const old = STATE.rows[sym];
        STATE.rows[sym] = old ? Object.assign({}, old, ch) : ch;
      }
      (m.removed || []).forEach((sym) => { delete STATE.rows[sym]; });
      Object.assign(STATE.lists, m.lists || {});
      Object.assign(STATE.cand, m.cand || {});
      (m.cand_removed || []).forEach((sym) => { delete STATE.cand[sym]; });
      Object.assign(STATE.meta, m.meta || {});
    }
    handleSnapshot(snapshotFromState(m.alerts || []));
  }
  async function poll() {
    let s;
    try {
      const res = await fetch(lastAlertId === null ? "/api/state" : `/api/state?alerts_since=${lastAlertId}`, { cache: "no-store" });
      s = await res.json();
    } catch (e) {
      $("banner").hidden = false; $("banner").className = "banner error";
      $("banner").textContent = "Cannot reach the screener server. Is app.py still running?";
      return;
    }
    handleSnapshot(s);
  }
  const startPolling = () => { if (!pollTimer) { poll(); pollTimer = setInterval(poll, POLL_MS); } };
  const stopPolling = () => { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } };
  function connect() {
    if (!("WebSocket" in window)) { pushMode = "polling"; renderPush(); startPolling(); return; }
    try { ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`); }
    catch { pushMode = "polling"; renderPush(); startPolling(); return; }
    ws.onopen = () => { wsRetry = 0; pushMode = "live"; renderPush(); stopPolling(); };
    ws.onmessage = (e) => { try { applyMessage(JSON.parse(e.data)); } catch (err) { console.error("push message failed", err); } };
    ws.onclose = () => {
      pushMode = "reconnecting"; renderPush(); startPolling();
      setTimeout(connect, Math.min(15000, 1000 * 2 ** wsRetry++));
    };
  }
  renderPush();
  connect();
})();
