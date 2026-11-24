// Backtest results: read-only views of the runs saved by `python backtest.py` (data/backtests/*.json).
(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const num = (v, d = 2) => (v === null || v === undefined || Number.isNaN(v)) ? "—"
    : Number(v).toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });
  const sgn = (v, d = 2, suf = "") => (v === null || v === undefined) ? "—"
    : `<span class="${v > 0 ? "up" : v < 0 ? "down" : ""}">${v > 0 ? "+" : ""}${num(v, d)}${suf}</span>`;
  const NAMES = { strong_momentum: "Strong Momentum", early_breakout: "Early Breakout", sector_leaders: "Sector Leaders" };
  let REP = null;

  const metricCells = (m) => !m || !m.trades ? '<td>0</td><td colspan="5" class="muted">no trades</td>'
    : `<td>${m.trades}${m.small_sample ? ' <span title="fewer than 30 trades: too few to tell skill from luck">⚠</span>' : ""}</td>
       <td>${num(m.win_rate, 1)}</td><td>${sgn(m.avg_net_ret_pct, 3, "%")}</td>
       <td class="muted">${m.ci95_net_ret_pct ? `${num(m.ci95_net_ret_pct[0], 2)} to ${num(m.ci95_net_ret_pct[1], 2)}` : "—"}</td>
       <td>${sgn(m.expectancy_rs, 0)}</td><td>${m.profit_factor ?? "—"}</td>`;

  function renderIntro() {
    const b = REP.backtest, c = b.costs, cov = REP.coverage || {};
    const exits = [b.stop_atr_mult ? `stop ${b.stop_atr_mult}× intraday ATR` : b.stop_pct ? `stop ${b.stop_pct}%` : "no stop",
      b.target_r ? `target ${b.target_r}R` : "no target", b.exit_on_lost_bars ? `exit after ${b.exit_on_lost_bars} closes without qualifying` : null,
      `square off ${b.exit_time}`].filter(Boolean).join(" · ");
    $("intro").innerHTML = `<p><strong>${esc(REP.label || REP.run_id)}</strong> · ${esc(REP.source)} · ${REP.symbols} stocks ·
      ${REP.sessions.length} sessions (${esc(REP.sessions[0] || "")} to ${esc(REP.sessions[REP.sessions.length - 1] || "")}),
      in-sample ${REP.in_sample_sessions.length} / out-of-sample ${REP.out_of_sample_sessions.length} · created ${esc(REP.created)} · config ${esc(REP.config_id || "")}</p>
      <p class="small">Signal: a strategy starts qualifying at a 5-min candle's close → long entry at the next candle's open; no entries after ${esc(b.last_entry)};
      ${b.max_trades_per_stock_day} trade per strategy, stock and day. Exits (user-defined, not recommendations): ${esc(exits)}.
      ₹${num(b.capital_per_trade, 0)} per trade; slippage ${c.slippage_bps} bps per side; brokerage ₹${c.brokerage_per_order} or ${c.brokerage_pct}% per order,
      STT ${c.stt_sell_pct}% (sell), exchange ${c.exchange_txn_pct}%, SEBI ₹${c.sebi_per_crore}/crore, stamp ${c.stamp_buy_pct}% (buy), GST ${c.gst_pct}%.</p>
      ${/DEMO/.test(REP.source) ? '<p class="down small"><strong>Demo data:</strong> simulated prices with trend days built in; these numbers only show the backtester works.</p>' : ""}
      ${cov.indices_without_5min && cov.indices_without_5min.length ? `<p class="small"><strong>Data gap:</strong> no 5-min candles for ${esc(cov.indices_without_5min.join(", "))},
        so sector RS (and RS vs NIFTY) is unavailable there and Strong Momentum / Sector Leaders are blocked for those stocks. Run <code>python history.py backfill</code>.</p>` : ""}
      <p class="muted small">${(REP.notes || []).map(esc).join(" ")}</p>`;
  }
  function renderCompare() {
    const head = `<thead><tr><th>Variant</th><th>Strategy</th><th colspan="6">In-sample</th><th colspan="6">Out-of-sample</th><th>Max DD ₹</th><th>MFE / MAE %</th></tr>
      <tr><th></th><th></th>${"<th>Trades</th><th>Win %</th><th>Avg net</th><th>95% CI</th><th>Exp. ₹</th><th>PF</th>".repeat(2)}<th>all</th><th>all</th></tr></thead>`;
    const rows = [];
    for (const v of REP.variants) {
      const items = [...Object.entries(v.by_strategy), ["combined", v.combined]];
      for (const [k, b] of items) {
        const is = b.in_sample, oos = b.out_of_sample, all = b.all;
        const warn = is && oos && is.trades && oos.trades && is.avg_net_ret_pct > 0 && oos.avg_net_ret_pct <= 0;
        rows.push(`<tr class="${warn ? "warnrow" : ""}" title="${warn ? "Positive in-sample, not out-of-sample" : ""}"><td>${esc(v.name)}</td>
          <td>${k === "combined" ? "<strong>all strategies</strong>" : esc(NAMES[k] || k)}</td>${metricCells(is)}${metricCells(oos)}
          <td>${all && all.trades ? num(all.max_drawdown_rs, 0) : "—"}</td><td>${all && all.trades ? `${sgn(all.avg_mfe_pct, 2)} / ${sgn(all.avg_mae_pct, 2)}` : "—"}</td></tr>`);
      }
    }
    $("cmp").innerHTML = head + `<tbody>${rows.join("")}</tbody>`;
  }
  const selTrades = () => REP.trades.filter((t) => t.variant === $("variant").value && ($("strategy").value === "all" || t.strategy === $("strategy").value));
  function renderEquity() {
    const tr = selTrades().slice().sort((a, b) => (a.day + a.exit_time).localeCompare(b.day + b.exit_time));
    $("eqTitle").textContent = `Equity curve: ${$("variant").value} · ${$("strategy").value === "all" ? "all strategies" : NAMES[$("strategy").value]} (net ₹, trade by trade)`;
    if (!tr.length) { $("eq").innerHTML = '<p class="muted">No trades.</p>'; return; }
    let cum = 0;
    const pts = [0, ...tr.map((t) => (cum += t.net_pnl))];
    const W = Math.max(320, $("eq").clientWidth || 900), H = 220, pad = 40;
    const lo = Math.min(0, ...pts), hi = Math.max(0, ...pts), span = hi - lo || 1;
    const x = (i) => pad + (i / (pts.length - 1 || 1)) * (W - pad - 10), y = (v) => 10 + (1 - (v - lo) / span) * (H - 30);
    const split = tr.findIndex((t) => t.sample === "out");
    $("eq").innerHTML = `<svg class="eq" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}">
      <line class="zero" x1="${pad}" x2="${W - 10}" y1="${y(0)}" y2="${y(0)}"/>
      ${split > 0 ? `<line class="split" x1="${x(split)}" x2="${x(split)}" y1="8" y2="${H - 18}"/><text x="${x(split) + 4}" y="18">out-of-sample →</text>` : ""}
      <path class="line" d="${pts.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("")}"/>
      <text x="2" y="${y(hi) + 4}">${num(hi, 0)}</text><text x="2" y="${y(lo) + 4}">${num(lo, 0)}</text>
      <text x="${pad}" y="${H - 4}">${esc(tr[0].day)}</text><text x="${W - 80}" y="${H - 4}">${esc(tr[tr.length - 1].day)}</text></svg>
      <p class="muted small">${tr.length} trades · net ₹${num(cum, 0)} · costs ₹${num(tr.reduce((s, t) => s + t.costs, 0), 0)} (already deducted)</p>`;
  }
  function renderBreakdowns() {
    const v = REP.variants.find((x) => x.name === $("variant").value);
    const k = $("strategy").value;
    const sources = k === "all" ? Object.entries(v.by_strategy) : [[k, v.by_strategy[k]]];
    const table = (title, key) => {
      const merged = {};
      for (const [, b] of sources) for (const [g, m] of Object.entries((b && b[key]) || {})) (merged[g] = merged[g] || []).push(m);
      const rows = Object.entries(merged).map(([g, ms]) => {
        const n = ms.reduce((s, m) => s + (m.trades || 0), 0);
        const avg = n ? ms.reduce((s, m) => s + (m.trades ? m.avg_net_ret_pct * m.trades : 0), 0) / n : null;
        const win = n ? ms.reduce((s, m) => s + (m.trades ? m.win_rate * m.trades : 0), 0) / n : null;
        const exp = n ? ms.reduce((s, m) => s + (m.trades ? m.expectancy_rs * m.trades : 0), 0) / n : null;
        return `<tr><td style="text-align:left">${esc(g)}</td><td>${n}${n < 30 ? " ⚠" : ""}</td><td>${num(win, 1)}</td><td>${sgn(avg, 3, "%")}</td><td>${sgn(exp, 0)}</td></tr>`;
      }).join("");
      return `<div><h3 style="font-size:14px">${esc(title)}</h3><div class="table-wrap"><table><thead><tr><th style="text-align:left">Group</th><th>Trades</th><th>Win %</th><th>Avg net</th><th>Exp. ₹</th></tr></thead><tbody>${rows || '<tr><td class="empty">none</td></tr>'}</tbody></table></div></div>`;
    };
    $("breakdowns").innerHTML = table("By entry time (IST)", "by_time_of_day") + table("By setup score at the signal", "by_setup_score")
      + table("By exit", "by_exit") + table("By sector", "by_sector");
  }
  function renderTrades() {
    const tr = selTrades();
    $("tradeCount").textContent = `(${tr.length}${tr.length > 500 ? ", first 500 shown" : ""})`;
    const cols = [["Day", (t) => esc(t.day)], ["Sample", (t) => t.sample === "out" ? "OOS" : "IS"], ["Strategy", (t) => esc((NAMES[t.strategy] || t.strategy).split(" ").map((w) => w[0]).join(""))],
      ["Symbol", (t) => `<strong>${esc(t.symbol)}</strong>`], ["Signal", (t) => esc(t.signal_time)], ["Entry", (t) => `${esc(t.entry_time)} ${num(t.entry)}`],
      ["Exit", (t) => `${esc(t.exit_time)} ${num(t.exit)}`], ["Why", (t) => esc(t.exit_reason)], ["Net %", (t) => sgn(t.net_ret_pct, 2)],
      ["Net ₹", (t) => sgn(t.net_pnl, 0)], ["Costs ₹", (t) => num(t.costs, 0)], ["R", (t) => t.r_multiple === null ? "—" : sgn(t.r_multiple, 2)],
      ["MFE / MAE %", (t) => `${sgn(t.mfe_pct, 2)} / ${sgn(t.mae_pct, 2)}`], ["Score", (t) => t.setup_score ?? "—"], ["Break", (t) => esc(t.breakout || "")],
      ["Sector", (t) => `<span class="muted">${esc(t.sector || "")}</span>`]];
    $("trades").innerHTML = `<thead><tr>${cols.map(([h]) => `<th>${h}</th>`).join("")}</tr></thead><tbody>${tr.slice(0, 500).map((t) => `<tr>${cols.map(([, f]) => `<td>${f(t)}</td>`).join("")}</tr>`).join("")}</tbody>`;
  }
  function renderAll() { renderEquity(); renderBreakdowns(); renderTrades(); }

  async function loadRun(id) {
    const res = await fetch(`/api/backtests/${encodeURIComponent(id)}`, { cache: "no-store" });
    if (!res.ok) { $("intro").innerHTML = `<p class="muted">Could not load ${esc(id)}.</p>`; return; }
    REP = await res.json();
    $("variant").innerHTML = REP.variants.map((v) => `<option>${esc(v.name)}</option>`).join("");
    $("strategy").innerHTML = '<option value="all">All strategies</option>' + REP.strategies.map((k) => `<option value="${esc(k)}">${esc(NAMES[k] || k)}</option>`).join("");
    $("meta").textContent = `${REP.trades.length} trades`;
    history.replaceState(null, "", `#${id}`);
    renderIntro(); renderCompare(); renderAll();
  }
  async function init() {
    const runs = await (await fetch("/api/backtests", { cache: "no-store" })).json();
    if (!runs.length) {
      $("intro").innerHTML = `<p>No saved backtests yet. Run one in a terminal, for example:</p>
        <pre>.venv/bin/python backtest.py --demo          # simulated history
.venv/bin/python backtest.py                 # your SQLite history (no network)</pre>`;
      return;
    }
    $("run").innerHTML = runs.map((r) => `<option value="${esc(r.run_id)}">${esc(r.created)} · ${esc(r.label || r.source)} · ${r.trades} trades</option>`).join("");
    const want = location.hash.slice(1);
    if (want && runs.some((r) => r.run_id === want)) $("run").value = want;
    loadRun($("run").value);
  }
  $("run").addEventListener("change", () => loadRun($("run").value));
  $("variant").addEventListener("change", renderAll);
  $("strategy").addEventListener("change", renderAll);
  init();
})();
