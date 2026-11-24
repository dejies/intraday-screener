// Stock detail chart: SVG candles + VWAP / EMA 9 / EMA 20 / OR and previous-day levels, volume, MACD,
// RSI and ADX panels, with a crosshair readout. Every value comes from /api/chart (computed on the
// server on completed candles); this file only draws. No third-party chart library is needed.
(() => {
  const NS = "http://www.w3.org/2000/svg";
  const fmt = (v, d = 2) => (v === null || v === undefined || !isFinite(v)) ? "—"
    : Number(v).toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function extent(arrays, extra = []) {
    let lo = Infinity, hi = -Infinity;
    for (const a of arrays) for (const v of a) if (v !== null && v !== undefined && isFinite(v)) { if (v < lo) lo = v; if (v > hi) hi = v; }
    for (const v of extra) if (v !== null && v !== undefined && isFinite(v)) { if (v < lo) lo = v; if (v > hi) hi = v; }
    if (lo === Infinity) return [0, 1];
    if (lo === hi) return [lo - 1, hi + 1];
    const padv = (hi - lo) * 0.04;
    return [lo - padv, hi + padv];
  }
  function ticks(lo, hi, n = 4) {
    const raw = (hi - lo) / n, mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || raw;
    const out = [];
    for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) out.push(v);
    return out;
  }
  function linePath(vals, x, y) {
    let d = "", pen = false;
    vals.forEach((v, i) => {
      if (v === null || v === undefined || !isFinite(v)) { pen = false; return; }
      d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
      pen = true;
    });
    return d;
  }

  function draw(el, data, readoutEl) {
    const c = data.candles || [];
    const s = data.series || {};
    if (!c.length) { el.innerHTML = '<p class="muted">No candles for this stock yet.</p>'; return; }
    const W = Math.max(320, Math.floor(el.clientWidth || 800));
    const pad = { l: 6, r: 62 };
    const panels = [["price", 250], ["vol", 46], ["macd", 78], ["rsi", 66], ["adx", 66]];
    const GAP = 8, AXIS = 18;
    let top = 6;
    const box = {};
    panels.forEach(([k, h]) => { box[k] = { t: top, h }; top += h + GAP; });
    const H = top + AXIS;
    const n = c.length, plotW = W - pad.l - pad.r, step = plotW / n;
    const bw = Math.max(1, Math.min(9, step * 0.68));
    const x = (i) => pad.l + step * (i + 0.5);
    const scale = (b, lo, hi) => (v) => b.t + b.h - ((v - lo) / (hi - lo)) * b.h;
    const opens = c.map((r) => r[1]), highs = c.map((r) => r[2]), lows = c.map((r) => r[3]), closes = c.map((r) => r[4]), vols = c.map((r) => r[5]);
    const lv = data.levels || {};
    const [plo, phi] = extent([highs, lows, s.vwap || [], s.ema9 || [], s.ema20 || []]);
    const yP = scale(box.price, plo, phi);
    const vmax = Math.max(1, ...vols);
    const yV = (v) => box.vol.t + box.vol.h - (v / vmax) * box.vol.h;
    const [mlo, mhi] = extent([s.macd || [], s.signal || [], s.hist || []], [0]);
    const yM = scale(box.macd, mlo, mhi);
    const yR = scale(box.rsi, 0, 100);
    const [alo, ahi] = extent([s.adx || [], s.plus_di || [], s.minus_di || []], [0, 30]);
    const yA = scale(box.adx, Math.min(0, alo), ahi);
    const parts = [];
    // grid + right axis labels
    const axis = (b, yf, vals, d) => vals.forEach((v) => {
      const yy = yf(v);
      if (yy < b.t - 1 || yy > b.t + b.h + 1) return;
      parts.push(`<line class="grid" x1="${pad.l}" x2="${W - pad.r}" y1="${yy.toFixed(1)}" y2="${yy.toFixed(1)}"/>`,
        `<text x="${W - pad.r + 4}" y="${(yy + 3).toFixed(1)}">${fmt(v, d)}</text>`);
    });
    axis(box.price, yP, ticks(plo, phi, 5), phi > 1000 ? 0 : 2);
    axis(box.macd, yM, [mlo * 0.8, 0, mhi * 0.8], 2);
    axis(box.rsi, yR, [30, 50, 70], 0);
    axis(box.adx, yA, [20, 25], 0);
    // session separators + time labels
    let prevDay = null, lastLabel = -99;
    c.forEach((r, i) => {
      const day = r[0].slice(0, 10), hm = r[0].slice(11, 16);
      if (prevDay !== null && day !== prevDay) {
        parts.push(`<line class="sess" x1="${(x(i) - step / 2).toFixed(1)}" x2="${(x(i) - step / 2).toFixed(1)}" y1="2" y2="${H - AXIS}"/>`,
          `<text x="${(x(i) - step / 2 + 3).toFixed(1)}" y="${H - 5}">${day.slice(8, 10)}/${day.slice(5, 7)}</text>`);
        lastLabel = i;
      } else if (i - lastLabel >= Math.max(8, Math.round(60 / step)) && (hm.endsWith("00") || hm.endsWith("30"))) {
        parts.push(`<text x="${(x(i) - 12).toFixed(1)}" y="${H - 5}">${hm}</text>`);
        lastLabel = i;
      }
      prevDay = day;
    });
    // levels (today's opening range, previous-day high/low)
    const level = (v, cls, label) => {
      if (v === null || v === undefined || v < plo || v > phi) return;
      const yy = yP(v).toFixed(1);
      parts.push(`<line class="lvl ${cls}" x1="${pad.l}" x2="${W - pad.r}" y1="${yy}" y2="${yy}"/>`,
        `<text x="${W - pad.r - 4}" y="${(yP(v) - 3).toFixed(1)}" text-anchor="end">${label} ${fmt(v)}</text>`);
    };
    level(lv.or_high, "lvl-or", "OR H"); level(lv.or_low, "lvl-or", "OR L");
    level(lv.pdh, "lvl-pd", "PDH"); level(lv.pdl, "lvl-pd", "PDL");
    // candles + volume
    let up = "", down = "", upW = "", downW = "", vu = "", vd = "", forming = "";
    c.forEach((r, i) => {
      const isUp = r[4] >= r[1];
      const xx = x(i), x0 = (xx - bw / 2).toFixed(1);
      const yo = yP(r[1]), yc = yP(r[4]);
      const yt = Math.min(yo, yc), hgt = Math.max(1, Math.abs(yc - yo));
      const body = `M${x0},${yt.toFixed(1)}h${bw.toFixed(1)}v${hgt.toFixed(1)}h${(-bw).toFixed(1)}Z`;
      const wick = `M${xx.toFixed(1)},${yP(r[2]).toFixed(1)}V${yP(r[3]).toFixed(1)}`;
      const vb = `M${x0},${yV(r[5]).toFixed(1)}h${bw.toFixed(1)}V${box.vol.t + box.vol.h}h${(-bw).toFixed(1)}Z`;
      if (r[6]) { forming += `<g class="forming ${isUp ? "up" : "down"}"><path d="${wick}"/><path d="${body}"/></g>`; }
      else if (isUp) { up += body; upW += wick; } else { down += body; downW += wick; }
      if (isUp) vu += vb; else vd += vb;
    });
    parts.push(`<path class="up" d="${upW}" fill="none"/><path class="down" d="${downW}" fill="none"/>`,
      `<path class="up" d="${up}"/><path class="down" d="${down}"/>${forming}`,
      `<path class="vol-up" d="${vu}"/><path class="vol-down" d="${vd}"/>`);
    // overlays
    if (s.vwap) parts.push(`<path class="l-vwap" d="${linePath(s.vwap, x, yP)}"/>`);
    if (s.ema9) parts.push(`<path class="l-ema9" d="${linePath(s.ema9, x, yP)}"/>`);
    if (s.ema20) parts.push(`<path class="l-ema20" d="${linePath(s.ema20, x, yP)}"/>`);
    // MACD
    if (s.hist) {
      const y0 = yM(0);
      let hu = "", hd = "";
      s.hist.forEach((v, i) => {
        if (v === null || v === undefined) return;
        const yy = yM(v), seg = `M${(x(i) - bw / 2).toFixed(1)},${Math.min(yy, y0).toFixed(1)}h${bw.toFixed(1)}v${Math.max(0.5, Math.abs(yy - y0)).toFixed(1)}h${(-bw).toFixed(1)}Z`;
        if (v >= 0) hu += seg; else hd += seg;
      });
      parts.push(`<path class="hist-up" d="${hu}"/><path class="hist-down" d="${hd}"/>`,
        `<path class="l-macd" d="${linePath(s.macd || [], x, yM)}"/><path class="l-signal" d="${linePath(s.signal || [], x, yM)}"/>`);
    }
    if (s.rsi) parts.push(`<path class="l-rsi" d="${linePath(s.rsi, x, yR)}"/>`);
    if (s.adx) parts.push(`<path class="l-pdi" d="${linePath(s.plus_di || [], x, yA)}"/><path class="l-mdi" d="${linePath(s.minus_di || [], x, yA)}"/>`,
      `<path class="l-adx" d="${linePath(s.adx, x, yA)}"/>`);
    // panel titles
    [["price", `${data.interval}-min candles · VWAP · EMA 9 · EMA 20`], ["vol", "Volume"], ["macd", "MACD (hist, line, signal)"],
     ["rsi", "RSI"], ["adx", "ADX · +DI · −DI"]].forEach(([k, t]) => parts.push(`<text x="${pad.l + 4}" y="${box[k].t + 11}" style="font-weight:600">${esc(t)}</text>`));
    parts.push(`<line class="xhair" id="xh" x1="0" x2="0" y1="2" y2="${H - AXIS}" visibility="hidden"/>`,
      `<rect x="${pad.l}" y="0" width="${plotW}" height="${H - AXIS}" fill="transparent" id="ov"/>`);
    el.innerHTML = `<svg class="chart" xmlns="${NS}" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img"
      aria-label="${esc(data.symbol)} ${data.interval}-minute chart">${parts.join("")}</svg>`;
    const svg = el.firstElementChild, xh = svg.querySelector("#xh"), ov = svg.querySelector("#ov");
    const show = (i) => {
      if (!readoutEl) return;
      if (i === null) { readoutEl.textContent = ""; xh.setAttribute("visibility", "hidden"); return; }
      const r = c[i], v = (k, d) => fmt(s[k] ? s[k][i] : null, d);
      xh.setAttribute("x1", x(i)); xh.setAttribute("x2", x(i)); xh.setAttribute("visibility", "visible");
      readoutEl.innerHTML = `<strong>${esc(r[0].slice(5))}</strong>${r[6] ? " (forming)" : ""} · O ${fmt(r[1])} H ${fmt(r[2])} L ${fmt(r[3])} C ${fmt(r[4])} · Vol ${fmt(r[5], 0)}`
        + ` · VWAP ${v("vwap")} · EMA9 ${v("ema9")} · EMA20 ${v("ema20")} · MACD ${v("macd", 3)} / ${v("signal", 3)} (hist ${v("hist", 3)})`
        + ` · RSI ${v("rsi", 1)} · ADX ${v("adx", 1)} (+DI ${v("plus_di", 1)} −DI ${v("minus_di", 1)})`;
    };
    const at = (evt) => {
      const rect = svg.getBoundingClientRect();
      const mx = (evt.clientX - rect.left) * (W / rect.width);
      return Math.max(0, Math.min(n - 1, Math.floor((mx - pad.l) / step)));
    };
    ov.addEventListener("mousemove", (e) => show(at(e)));
    let lastDone = n - 1;
    while (lastDone > 0 && c[lastDone][6]) lastDone--;              // the forming candle has no indicator values yet
    ov.addEventListener("mouseleave", () => show(lastDone));
    ov.addEventListener("touchmove", (e) => { if (e.touches[0]) show(at(e.touches[0])); }, { passive: true });
    show(lastDone);
  }

  window.Charts = { draw };
})();
