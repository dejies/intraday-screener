# Gap analysis: current project vs `CLAUDE_SKILL_NSE_INTRADAY_SCANNER.md`

*Prepared 24-Nov-2026. Per §13 and §15 of the spec, this is the first task: inspect the existing
project, map it to milestones M0–M11, and plan the next steps without rewriting working code.*

## 1. Summary (updated 25-Nov-2026 after steps 1–8: all milestones done)

**Done:**
- **Data layer:** Dhan REST and WebSocket clients (decoding checked against the official SDK),
  stale-feed detection, SQLite history.
- **Universe:** `watchlist.csv`, all 3,226 NSE stocks with sectors (99.4% mapped).
- **Sector strength:** NIFTY 50 + 10 sector indices, and 12 composites. Sector RS vs Nifty, stock RS
  vs sector, percentiles and breadth.
- **Indicators:** EMA (5-min and daily) with alignment checks and EMA20 slope, MACD with states and
  15-min context, RSI 14, ADX/DI with ADX rising, daily and intraday ATR, VWAP with distance, slope and
  reclaim/loss events, cumulative RVOL and same-slot 5-min RVOL (20 sessions, min 10, with diagnostics),
  Bar RVOL, optional Bollinger/OBV/MFI/CPR. Completed candles only; indicator snapshots saved to SQLite.
- **Strategies (step 4):** Strong Momentum, Early Breakout and Sector Leaders, each with its own config,
  reason codes and missing-data policy; an explainable 100-point setup score with bounded components and
  an effective denominator; candidate lifecycle (new / active / weakening / removed, grace period) with
  an audit trail; evaluations, candidates, events and the strategy config persisted (schema v5).
- **Alerts (step 5):** an alert centre for every type in the spec (strategy qualify / lose / remove, score
  crossings, RVOL crossing, stock and sector RS turning positive, top of sector, MACD bullish cross, VWAP
  reclaim / loss, breakouts, volume spikes, feed down / stale / recovered, data and credential failures),
  generated server-side from state transitions with severity, unique keys, cooldowns and hysteresis,
  persisted with acknowledgement (schema v6), filterable history; toasts, per-type sound (muted by
  default) and notifications in the browser.
- **Dashboard (step 6):** FastAPI + uvicorn with **incremental WebSocket pushes** (keyed state, only changed
  fields, one shared snapshot per tick, no backlog) and a polling fallback; tables patched cell by cell so
  the selected row survives; sortable columns; filters by sector, strategy, score, RVOL and direction;
  strategy tabs + All candidates + Alert history; sector panel with index values; a **stock detail panel**
  with server-computed 1m/5m/15m candles, VWAP / EMA / levels, MACD, RSI and ADX panels, RS comparison, RVOL
  baselines, gates, alerts and data quality; a **settings page** (validated, live-applied, persisted, no
  credentials); `/health`; local-only guards (trusted host, JSON-only POST, WebSocket origin check).
- **Backtesting (step 7):** `backtest.py` replays saved history candle close by candle close through the
  same feature, relative-strength, filter, strategy and score code (verified equal to the live engine, and
  a no-look-ahead test), simulates long trades with user-defined exits, Indian intraday charges and
  slippage, and reports win rate, expectancy, profit factor, drawdown, MFE/MAE and breakdowns, in-sample vs
  out-of-sample, per settings variant; saved to `backtest_runs` / `backtest_trades`, JSON / Markdown, and the
  `/backtests` page.
- **Hardening (step 8):** NSE holiday / special-session calendar (`nse_holidays.csv`), an explicit IST
  clock, JSON logs with run / request / cycle ids and secret redaction, a silent-feed watchdog that forces a
  reconnect, browser security headers (CSP, frame denial), `pyproject.toml`, run scripts, `manage.py check`
  and `backup`, recovery tests, [OPERATIONS.md](OPERATIONS.md) and [SECURITY_REVIEW.md](SECURITY_REVIEW.md).
- **Tests:** a simulator and 216 tests.

**Still pending:**
Nothing from M0–M11 is pending. Open items, by choice or because they need the live feed:
1. **Optional alert channels** (email / Telegram): the alert centre has a `sinks` hook; not built.
2. **Live-feed items to confirm on the first live day:** the real Dhan WebSocket end to end (tested only
   against a fake server), and bucketing 1-min candles by exchange time (LTT) instead of arrival time.
3. **Live-built 1-min candles aren't stored**; REST 5-min / 1-min candles are.
4. **Timestamps stay naive IST** (decision h below, revised): NSE runs on one timezone with no DST, so the app
   stores IST wall-clock time, takes "now" from an explicit IST clock, and puts UTC in the logs.

## 2. Milestone-by-milestone status

Legend: ✅ done · 🟡 partial · ❌ missing

| Milestone | Status | What already exists | Gaps against the spec |
|---|---|---|---|
| **M0** Foundation | 🟢 done | `config.py` + the settings page over `data/settings.json`, `.env.example`, `.gitignore`, README, SQLite with versioned schema and migrations, **FastAPI** app with `/health`, static HTML / CSS / JS, **structured logging** in `logs/`, **`pyproject.toml`** (step 8). | Settings are Python + JSON rather than YAML, and the package stays flat (`screener/`) rather than the suggested layered layout (decision f): both work and changing them would only move code. |
| **M1** Universe & sectors | 🟢 mostly done (24-Nov-2026) | **`watchlist.csv`**, built by `universe.py build`, has every NSE-listed stock (3,226: main board + SME, from NSE's official lists) with company, series, ISIN, listing date, Dhan ID, and a **sector in NSE's 22-sector names**. Sources: the NSE Indices Nifty Total Market file (~750 stocks); otherwise Dhan's Company Info API (`/v2/data/companyinfo`), translated to NSE sectors only where the overlap agrees ≥ 75%. Every row records its source and confidence; unmatched rows stay *unmapped*. Company Info is cached and resumable. Tests: `tests/test_universe.py`. | Map sectors to sector indices (needed for step 2's sector RS). Nifty 500 and F&O presets. Active/inactive flag. |
| **M2** Dhan data | 🟢 done (heartbeat watchdog in step 8) | REST client (rate limits, retries, error codes) and WebSocket client (batches of 100, reconnect, resubscribe, fatal disconnects, token redaction). **NIFTY 50 + 10 NSE sector indices** subscribed (ticker) or quoted over REST. **Stale-feed detection**: DATA STALE / FEED DISCONNECTED states, banner and badge. Demo feed + fake-server test. | Typed response validation; a run against the real feed. The client-side heartbeat check is done (silent-feed watchdog, step 8). |
| **M3** Candles & persistence | 🟡 | 1-min candles built from ticks (cumulative-volume deltas, late packets, reconnect baseline). REST 5-min/1-min candles cached in SQLite with coverage tracking and recovery. 15-min candles resampled from completed 5-min ones (09:15-aligned) for the MACD context (25-Nov-2026). | 15-min candles aren't stored. Live candles are bucketed by arrival time, not exchange time (a deliberate choice, see §4e). Live-built candles aren't persisted. Holiday / special-session calendar done in step 8. Timestamps are stored as naive IST (see decision h). |
| **M4** History & RVOL | 🟢 done (25-Nov-2026) | `cumulative_rvol()`: today's volume to the last **completed** candle vs the previous 20 sessions to the same time; **unavailable below 10 valid sessions**; numerator, denominator, cutoff and session count in the tooltip. `slot_rvol()`: **same-slot 5-min candle RVOL** (a traded session missing the slot counts 0), with the forming candle shown separately as **LIVE / INCOMPLETE**. NumPy implementation shared by both. 35-day warm-up. Bar RVOL alerts unchanged. Tests in `tests/test_momentum_volume.py`. | Cumulative RVOL > 2.5 as the strategies' **primary gate** belongs to step 4 (today it's a 1.5× strength point and a 2.5× highlight). Holidays are handled implicitly (only sessions with data count), not from a calendar. |
| **M5** Indicators | 🟢 done (25-Nov-2026) | All on **completed candles**, each published only with enough history (else "—"). EMA 9/20/50/200 (5-min, daily) + **alignment checks and EMA20 slope**. MACD 12/26/9 with line/signal/histogram, above zero, above signal, histogram rising and the **four state classes**, plus **15-min context**. **RSI 14** (Wilder, SMA-seeded; exact match with the published example) and slope. ADX/+DI/−DI with **ADX rising**. Daily and **intraday ATR %**. VWAP with **distance %, slope, and reclaim/loss events** (0.1% buffer, 60 s confirmation). Optional **Bollinger (20, 2) bandwidth/squeeze, OBV trend, MFI 14, pivots/CPR**. **Indicator snapshots** per completed candle in SQLite (`indicator_snapshots`, schema v4). Formulas and worked examples in `technical_indicators_calculations.md` §3–§9, §11, §14, §17. | Using these in the **votes/score** (e.g. "price above VWAP *and* VWAP rising", RSI 55–70) is step 4. VWAP reclaim/loss and MACD crossings as **alerts** are step 5. |
| **M6** Relative strength & breadth | 🟢 done (25-Nov-2026) | `screener/relative_strength.py` + versioned `sector_indices.csv`. **Sector RS vs Nifty** and **stock RS vs sector** over 5/15/30/60 min + day, from a common 5-minute-grid start; never zero for missing data. 10 sectors measured by NSE sector indices, 12 by a median-of-liquid-stocks composite (documented). Percentile within sector, sector ranks, rising vs "falling less" states, market breadth. Sector panel + columns in the UI. Tests: `tests/test_relative_strength.py`. | Optional: a cap-weighted composite (Dhan market cap is cached). |
| **M7** Strategies & ranking | 🟢 done (25-Nov-2026) | `screener/strategies.py`: **Strong Momentum, Early Breakout, Sector Leaders** with the spec's gates, each with its own block in `config.STRATEGIES` (enable, thresholds, RS horizon, `skip_if_missing`) and reason codes (`CUM_RVOL`, `LEVEL_BREAK`, …). Statuses qualified / failed / blocked (missing data, explained) / filtered / no_data / disabled. Early Breakout: first completed 5-min close above OR high (only once complete), PDH or a tight consolidation high, fresh within 30 min. **Hard filters first** (turnover (off by default since 25-Nov-2026: `MIN_AVG_TURNOVER_CR = 0`), ATR, series, optional price band `MIN_PRICE` / `MAX_PRICE` on the previous close, added 25-Nov-2026). `screener/scoring.py`: **100-point setup score**, spec weights, bounded linear components, effective denominator, labels, "insufficient data" below 70 scorable points; called a setup score, never a probability. `screener/candidates.py`: **lifecycle** new → active → weakening → removed (30 s confirmation, 10-min grace, removed shown 30 min) with events. **Persistence** (schema v5): `strategy_config`, `scanner_candidates` (restored after restart), `candidate_events` (deduplicated audit trail), `scanner_evaluations` (per stock per completed candle), 60-day retention. UI: strategy tabs, candidate table, change log, `/api/explain/<symbol>` "why?" view. Tests: `tests/test_strategies.py` (37). | Long-side only, as in the spec (bearish stays on the votes tab). Thresholds and weights are the spec's initial values, not backtested (step 7). No settings page (step 6). Strategy transition **alerts** are step 5. |
| **M8** Alerts | 🟢 done (25-Nov-2026) | `screener/alerts.py`: **AlertCentre** (unique event keys, per-subject cooldown, SQLite persistence, restore after restart, acknowledgement, filters, `sinks` hook for future channels), **AlertRules** (state-transition rules with first-look baselines and hysteresis for every alert type in spec §8; values snapshot and reason codes on every alert; scopes so 500 stocks don't flood it), **SystemMonitor** (feed disconnected / stale / stopped for 10 s → CRITICAL, recovery → LOW), data-error folding and credential alerts. **Severity** CRITICAL / HIGH / MEDIUM / LOW per `config.ALERT_RULES`. Schema v6 (severity, category, strategy, alert_key unique index, count, acknowledged_at; legacy rows backfilled). API: `GET /api/alerts` (day / severity / category / strategy / symbol / kind / unacked), `POST /api/alerts/ack` (JSON only), incremental `?alerts_since`. UI: **toasts** (sticky for CRITICAL/HIGH), **alert centre** with filters, date history and acknowledgement, 🔔 unread badge, system alerts styled apart, **per-type toast / sound settings** (sound muted until enabled), notifications after permission, row highlighting. `history.py alerts` filters. Tests: `tests/test_alerts.py` (21). | Email / Telegram / other channels (optional per spec). Thresholds and cooldowns are first guesses to tune after live sessions. |
| **M9** Dashboard | 🟢 done (25-Nov-2026) | **FastAPI + uvicorn** (`app.py`), **WebSocket `/ws`** with incremental deltas (`screener/push.py`: keyed rows, changed fields only, lists / candidates / meta / new alerts; one snapshot per tick shared by all browsers, each gets only the newest state, so no unbounded queue) and a polling fallback. Frontend split into `static/css/app.css`, `static/js/app.js`, `static/js/chart.js`: **keyed tables patched cell by cell** (selected row preserved), **sortable columns**, **filters** (sector, strategy, score, RVOL, direction), tabs **All candidates / Strong Momentum / Early Breakout / Sector Leaders / votes / Alert history**, header with NIFTY level, push state, feed state and last tick. **Stock detail panel**: SVG candles (1m/5m/15m) with VWAP, EMA 9/20, OR and PDH/PDL lines, volume, MACD, RSI, ADX panels and a crosshair readout, all series computed server-side (`engine.chart_data`); quote, gates and score parts, stock / sector / Nifty returns per horizon, RVOL baselines, indicators, levels, alert timeline, data quality (`engine.stock_detail`). **Settings page** (`/settings`, `screener/settings.py`): ~140 fields in 8 groups, validated together, live-applied or flagged *restart*, saved to `data/settings.json`, no credentials. `/health`. Local-only guards: trusted Host, JSON-only POST, WebSocket Origin check. Checked at 1440 px and a 390 px phone through Chrome DevTools with no console errors. Tests: `tests/test_dashboard.py` (17). | Market state HOLIDAY (needs the holiday calendar, step 8). Quiet hours. The chart has no zoom / pan. |
| **M10** Backtesting | 🟢 done (25-Nov-2026) | `screener/backtest.py` + `backtest.py`: **historical replay** of every 5-min candle close from SQLite (or `--demo`, or `--fetch` from Dhan); **no look-ahead** (causal feature arrays, decisions from candles up to the close, entry at the next open; a test removes later candles and checks earlier decisions don't change; features verified equal to `compute_intraday`); **configurable fees and slippage** (brokerage, STT, exchange, SEBI, stamp duty, GST; labelled as approximations to check); user-defined exits (ATR or % stop, R target, lost eligibility, square-off); **MFE / MAE, win rate, average return with 95% CI, expectancy, profit factor, drawdown, time-of-day / sector / setup-score / exit breakdowns**; **in-sample vs out-of-sample** split in time and **settings variants** compared side by side (validated like the settings page). Schema v7 `backtest_runs` / `backtest_trades`, reports in `data/backtests/`, read-only `/backtests` page. `history.py backfill` now also fetches the sector indices. Tests: `tests/test_backtest.py` (17). | Decisions at candle closes only; survivorship bias (today's universe and sectors); no partial fills or liquidity model; no automatic parameter search (variants are listed by hand, which keeps the comparison honest). Your database currently has ~23 sessions and no index 5-min candles: run `history.py backfill --watchlist nifty50.txt --days 180` for a meaningful test. |
| **M11** Hardening | 🟢 done (25-Nov-2026) | **Full test suite** (216 tests). **Recovery and restart**: restart restores alerts / acks / candidates without re-firing; the engine loop survives failing data calls; a silent live feed is force-reconnected (`FEED_RECONNECT_STALE_SECONDS`); missing / old databases are rebuilt or upgraded; bad settings are skipped; recovery test plan in OPERATIONS.md. **Logging and diagnostics**: `screener/logs.py` (console + rotating JSON lines, `run_id` / `request_id` / `cycle`, redaction of tokens, headers and JWTs, tracebacks included), `X-Request-ID`, `manage.py check` (Python / packages, `.env` privacy, credential presence and JWT expiry without printing, files, settings, holiday coverage, schema + `quick_check`, candle coverage, disk, port / health, log errors), `/health`. **Packaging / run scripts**: `pyproject.toml` (deps, console scripts, pytest), `scripts/setup.sh`, `start.sh`, `demo.sh`, `backup.sh`. **User guide and troubleshooting**: README + [OPERATIONS.md](OPERATIONS.md). **Security review** ([SECURITY_REVIEW.md](SECURITY_REVIEW.md)) and **configuration backup** (`manage.py backup`: online SQLite backup, settings, data files; `.env` only on request). Calendar: `nse_holidays.csv` + `screener/market_calendar.py` (holidays, special sessions, HOLIDAY state); `screener/clock.py`. Tests: `tests/test_hardening.py` (16). | Exchange-time (LTT) bucketing and the real-feed checks wait for a live run; email / Telegram channels optional. |

## 3. Facts checked against Dhan sources

| Fact | Source | Checked |
|---|---|---|
| REST paths `/v2/charts/historical`, `/v2/charts/intraday` (≤ 90 days per request), `/v2/marketfeed/ltp`; headers `access-token`, `client-id` | dhanhq.co/docs/v2/historical-data/, /market-quote/ | 24-Nov-2026 |
| WebSocket URL, subscribe JSON (≤ 100 per message), 5,000 instruments per connection, 5 connections | dhanhq.co/docs/v2/live-market-feed/ | 24-Nov-2026 |
| Binary layout (header `<BHBI`, Quote `<BHBIfHIfIIIffff`, Prev Close, Disconnect) and IST-encoded LTT | `dhanhq` SDK 2.2.0 `marketfeed.py` (source read, not run) | 24-Nov-2026 |
| Rate limits: Data 5/s, Quote 1/s | dhan.co support FAQ | 24-Nov-2026 |
| 3,213 NSE equity shares (type `ES`, series EQ 2,419 / SM 479 / BE 180 / ST 102 / BZ 32 / SZ 1). The EQ series also holds 361 ETFs and 22 test symbols. | Dhan instrument list (`api-scrip-master.csv`) | 24-Nov-2026 |
| Index IDs (segment `IDX_I`): NIFTY 13, BANKNIFTY 25, FINNIFTY 27, NIFTY IT 29, AUTO 14, PHARMA 32, FMCG 28, METAL 31, REALTY 34, ENERGY 42, MEDIA 30, PSU BANK 33, PVT BANK 15, HEALTHCARE 447, CONSR DURBL 466, OIL AND GAS 470, INFRA 43, COMMODITIES 39, CONSUMPTION 40, SERV SECTOR 46, IND DEFENCE 493, Capital Mkt 803, NIFTY 500 19, MIDCAP 150 1, SMALLCAP 250 3 | Dhan instrument list | 24-Nov-2026 |
| F&O stocks can be listed from Dhan's `FUTSTK` contracts: 236 underlyings, including test symbols to exclude | Dhan instrument list | 24-Nov-2026 |
| **Not in Dhan's list:** sector/industry per stock, index constituents, ISIN (compact file) | Dhan instrument list | 24-Nov-2026 |
| **Not yet verified:** that the live feed streams the sector indices on your plan; the real WebSocket feed end to end | — | needs a live run |

## 4. Where the spec and the current design differ (decisions)

| # | Topic | Current | Spec | Proposed default |
|---|---|---|---|---|
| a | **Sector mapping source** | none | explicit, versioned mapping; never fabricated | **Needs your choice** (see the end of this file) |
| b | Scoring | 7 votes, score /9, bullish/bearish lists | 3 strategies + 100-point setup score | Add the strategies and the 100-point score as the primary view. Keep the vote bias as a column (*Bias*) for continuity. |
| c | RVOL | Day RVOL (cumulative, 20 sessions) as a 1.5× strength point; Bar RVOL (1-min vs previous 20 candles) ⚡ alerts | cumulative RVOL > 2.5 as the primary gate; 5-min same-slot RVOL > 2.0 | Keep the cumulative formula and use 2.5 inside the strategies. **Add** same-slot 5-min RVOL. Keep the Bar RVOL ⚡ alert as an optional alert type (you asked for it earlier). |
| d | Web framework | ~~Flask + 3-second polling~~ FastAPI + WebSocket push (step 6) | FastAPI + WebSocket push preferred | Done in step 6; polling remains only as the fallback when the WebSocket is down. |
| e | Candle time | live 1-min candles bucketed by **arrival time** | exchange timestamps | Bucket by **exchange LTT**, checking it against the IST clock, and fall back to arrival time if they disagree. |
| f | Folder layout | flat `screener/` package | layered package (`data/`, `indicators/`, `strategies/` …) | Move modules into the layered layout **as each milestone touches them**, not in one big move, so tests stay green. |
| g | Holidays | done (step 8): `nse_holidays.csv` | must be handled | Versioned file at the project root, updated each December from NSE's circular; the 2026 rows were cross-checked between two broker pages because NSE's PDF didn't load automatically. |
| h | Timestamps | IST text in SQLite | UTC internally, IST in the UI | **Revised in step 8:** keep naive IST storage (NSE is single-timezone, no DST), take "now" from an explicit IST clock (`screener/clock.py`) so the computer's timezone can't shift sessions, and log UTC + IST. |

## 5. Proposed order of work

Each step will be implemented and tested, then the README and this file updated, and then **you
review it before the next step starts**.

| Step | Milestones | Scope | Main files |
|---|---|---|---|
| 1 | M1 | Sector mapping (versioned CSV + repository + coverage report), universe presets (All / Nifty 500 / F&O), ISIN where available | new `screener/sectors.py`, `data/sector_mapping_v1.csv`, `dhan_client.py` |
| 2 | M2 + M6 | Subscribe the sector indices. Sector RS vs Nifty and stock RS vs sector over 5/15/30/60-min and session horizons. Percentile within sector. Market breadth. Stale-feed detection. | `live_feed.py`, new `screener/relative_strength.py`, `live_engine.py` |
| 3 | M4 + M5 | RSI 14, MACD states, VWAP distance/slope/events, ADX rising, EMA20 slope, 15-min context, same-slot 5-min RVOL, RVOL diagnostics and minimum sessions | `indicators.py`, `engine.py` |
| 4 | M7 | Three strategies (own config + reason codes), 100-point score, candidate lifecycle, persistence | new `screener/strategies/`, `screener/scoring.py`, `store.py` (schema v3) |
| 5 | M8 | Alert centre: severity, acknowledgement, filters, strategy and system alerts, toasts | `engine.py` alerts → `screener/alerts/`, `store.py`, UI |
| 6 | M9 | FastAPI + WebSocket push, tabs, sector panel, detail panel with charts, sortable/filterable tables, settings page | `app.py`, `static/` |
| 7 | M10 | Backtest/replay on the SQLite history, with costs, MFE/MAE and out-of-sample comparison | new `backtest/` |
| 8 | M11 | Holiday calendar, structured logging, `pyproject.toml`, run scripts, recovery tests, security review | various |

## 6. Decision taken for step 1 (sector mapping)

Your choice (24-Nov-2026): start from all NSE-listed stocks, use NSE's own sector mapping where it's
available, and find the rest elsewhere. Implemented as described in the README section
*Stock universe and sectors*. The options considered before that were:

Dhan's instrument list has no sector or industry field, and the spec forbids inventing a mapping.
The options:

1. **NSE's official Nifty 500 constituents file** (`ind_nifty500list.csv` on niftyindices.com),
   which has an *Industry* column. It's saved once into the project as a versioned file and refreshed
   after each half-yearly index rebalance. Industries map to the Dhan sector indices above (e.g.
   *Information Technology* → NIFTY IT 29).
   - Covers ~500 stocks. The rest show sector RS as *unavailable*, or use NIFTY 500 as a broader
     benchmark, clearly labelled.
   - The same file gives the Nifty 500 preset. Midcap 150 and Smallcap 250 are already inside the Nifty 500.
2. **Sectoral-index membership files** (NIFTY IT, BANK, AUTO … constituents). These are an exact
   match to each sector index, but cover fewer stocks (~250) and some stocks sit in several indices.
3. **Your own CSV** (`symbol,sector`), if you already have a mapping you trust.
