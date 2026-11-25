# Intraday Screener (learning project)

A local dashboard that screens a watchlist (default: NIFTY 50) on 5-minute candles,
checks every stock against three scanner strategies (**Strong Momentum, Early Breakout, Sector
Leaders**) and ranks the candidates by an explainable **0–100 setup score**. It also sorts stocks into
**Bullish / Bearish / Neutral** using VWAP, **MACD** and the
**Directional Movement Index (+DI, −DI, ADX)**, and **pops up an alert when a stock
breaks out** of its opening range, previous-day high/low or 52-week high/low, or
**when a 1-minute candle's volume is more than 2.5× its recent average (Bar RVOL)**.
Only NSE equity shares are screened: either your watchlist, or
**every NSE stock (3,213 in Dhan's instrument list) streamed over Dhan's WebSocket feed**.

Data comes from the DhanHQ v2 REST API and Live Market Feed (read-only). **This tool never places orders.**
It is for learning, not investment advice.

## Configure and start the application

### 1. What you need

| Need | For |
|---|---|
| **Python 3.9 or newer** (`python3 --version`) | everything |
| A browser (Chrome, Safari, Edge or Firefox) | the dashboard |
| A **Dhan account with a Data API subscription** | live data only; demo mode needs nothing. The basic *Data API* plan (₹499 + GST a month) covers every feature here, so *Pro Traders* isn't needed. |

### 2. Install (once)

The short way: `scripts/setup.sh` (creates `.venv`, installs everything, makes a private `.env` from the
template and runs `manage.py check`). Then `scripts/demo.sh` or `scripts/start.sh`. By hand:

```bash
cd intraday-screener
python3 -m venv .venv                      # private Python environment for this project (3.12+ recommended)
.venv/bin/pip install -r requirements.txt  # pandas, numpy, fastapi, uvicorn, requests, websocket-client, pytest, ...
.venv/bin/python -m pytest -q              # optional: runs the 183 tests (~2 minutes)
```

On Windows, use `.venv\Scripts\python` wherever this README says `.venv/bin/python`.

### 3. Try it first without an account (demo mode)

```bash
.venv/bin/python app.py --demo
```

Your browser opens <http://127.0.0.1:8050>. Prices are simulated random walks on a clock running
20× faster than real time, so you can watch a whole session, including breakouts and ⚡ alerts, in
about 20 minutes. Demo data is never saved. Useful variations:

```bash
.venv/bin/python app.py --demo --demo-start 09:25                 # just before the opening range completes
.venv/bin/python app.py --demo --demo-speed 60                    # faster clock
.venv/bin/python app.py --demo --universe nse --demo-universe-size 3213   # all-NSE mode at full size
```

### 4. Connect your Dhan account

1. **Subscribe** at web.dhan.co → *Profile* → *Get Trading & Data APIs* → *Data APIs*. Without an
   active subscription, Dhan answers with error `806`.
2. **Generate an access token** on the same page, and note your **client ID**.
3. **Create `.env`** in the project folder:
   ```bash
   cp .env.example .env        # Windows: copy .env.example .env
   ```
   Then fill in both lines:
   ```ini
   DHAN_CLIENT_ID=1000000001
   DHAN_ACCESS_TOKEN=eyJ0eXAiOiJKV1Qi...
   ```
4. **Keep the token private.**
   - Put it in `.env` only (git ignores that file). Never put it in code, chats or commits.
   - Environment variables with the same names override `.env`.
   - Tokens expire after 24 hours. The running app **renews the token itself** (see
     [Access-token auto-renewal](#access-token-auto-renewal)), so you only generate one by hand the
     first time and after a gap of more than a day with the app stopped. Paste a new token into `.env`
     and the running app loads it within a minute; no restart needed.

### 5. Build the stock list with sectors (`watchlist.csv`)

The default watchlist is `watchlist.csv`: **every NSE-listed stock (~3,226) with its sector**.
Build it once, and again whenever you want fresh listings:

```bash
.venv/bin/python universe.py build      # ~14 min the first time (one Dhan request per stock), seconds afterwards
.venv/bin/python universe.py report     # coverage: stocks per sector, how each sector was found
```

See [Stock universe and sectors](#stock-universe-and-sectors-watchlistcsv) for where each column
comes from. Without Dhan credentials, `universe.py build --no-dhan` still builds the list, with NSE
sectors for the ~750 index stocks.

### 6. Choose what to scan

| Mode | Command | Stocks | Live prices from |
|---|---|---|---|
| **Watchlist** (default) | `.venv/bin/python app.py` | every stock in `watchlist.csv` (~3,226, with sectors) | Dhan WebSocket (any list over 100 stocks) |
| Small list over REST | `.venv/bin/python app.py --watchlist nifty50.txt` | the NIFTY 50 | REST quotes every 5 s |
| **All NSE stocks** | `.venv/bin/python app.py --universe nse` | every NSE equity share in Dhan's list (3,213); sectors come from `watchlist.csv` | Dhan WebSocket |

- **Your own list:** a `.txt` file with one NSE symbol per line (`#` starts a comment), or a `.csv`
  with a `symbol` column (plus optional `sector`), passed with `--watchlist mylist.txt`.
- **Feed choice:** lists of up to 100 stocks use REST polling unless you add `--feed ws`. Bigger lists
  always use the WebSocket (`REST_MAX_SYMBOLS` in `config.py`).
- **Demo mode** uses `nifty50.txt` unless you pass `--universe nse` or another `--watchlist`.

### 7. Start it

**When to start:**

| Mode | Start by | Why |
|---|---|---|
| Watchlist | ~09:10 | indicators are ready before 09:15 and the opening range is tracked from the first candle |
| All NSE | ~08:45, or run the pre-market backfill below | daily history for ~3,200 stocks takes ~14 minutes the first time and each morning. ⚡ alerts begin ~21 minutes after the feed connects, so connect before 09:15 to have them from 09:35. |

**Pre-market backfill for all-NSE mode** (optional; afterwards the app starts in seconds):

```bash
.venv/bin/python history.py backfill --universe nse --daily-only
```

**Start the dashboard:**

```bash
.venv/bin/python app.py                    # or --feed ws / --universe nse
```

The terminal prints a line like
`Dashboard on http://127.0.0.1:8050 (source: Dhan (live), universe: watchlist, 50 symbols, history: market.sqlite)`
and opens your browser. Use `--no-browser` to skip that, then open the address yourself.
The server listens on your machine only (`127.0.0.1`).

**In the browser:**

1. Click **Enable sound & notifications** once, and allow notifications if the browser asks. Sound
   stays off until you do this (or click **Sound**) after each page load, because browsers block autoplay.
2. Click **Test alert**: a toast should appear at the bottom right, with a beep if sound is on, plus a
   desktop notification if the tab is in the background.
3. Open **Alert settings** to choose which alert types pop up as a toast, play a sound or notify, and the
   minimum severity for each. *Breakouts: only confirmed ones* is recommended in all-NSE mode.
4. Leave the tab open. The server pushes changes over a WebSocket about once a second (the
   **live push** badge); if that connection drops, the page polls every 3 s until it's back. Only
   changed cells are redrawn, so a row you've selected stays selected. Click any row or symbol for the
   stock's chart and details; click a column header to sort; use the filter bar and the search box.

**Stop it** with **Ctrl+C** in the terminal. Candles and alerts are saved as they arrive, so
nothing is lost. Restarting the same day restores the day's alerts (with their acknowledgements).

**Without the dashboard:** `.venv/bin/python app.py --once` runs one watchlist scan, prints the
bullish and bearish tables in the terminal, and exits.

### 8. A typical trading day

| Time (IST) | What to do |
|---|---|
| Before 08:45 | Only after a weekend or a day off with the app stopped: the token may have expired, so generate a new one and put it in `.env` (the header's **token** badge, or `manage.py check`, shows how long it has left) |
| Once a week | `universe.py build` to pick up new listings (only new or stale stocks are re-fetched) |
| 08:30–08:45 | *All-NSE only:* `history.py backfill --universe nse --daily-only` |
| 08:45–09:10 | Start `app.py`, enable alerts, click **Test alert** |
| 09:15–15:30 | Watch the strategy tabs, the candidate changes and the alerts; click a symbol to see why it is (or isn't) a candidate. In all-NSE mode the banner shows history loading until it finishes. |
| After 15:30 | Review with `history.py alerts --days 1`, then stop with Ctrl+C. Left running, it slows its polling after the close. |

### 9. Configuration reference

**Files:**

| File | What it holds |
|---|---|
| `.env` | your Dhan client ID and access token |
| `watchlist.csv` | every NSE stock with its sector (built by `universe.py build`) |
| `nifty50.txt` | the NIFTY 50, a small list for REST mode and testing |
| `sector_indices.csv` | which NSE sector index measures each sector (or *composite*) |
| `data/sources/` | the NSE files `universe.py` downloaded, with dates in `sources.json` |
| `config.py` | every setting: thresholds, periods, polling, universe, port |
| `security_ids_override.csv` | optional `symbol,security_id` rows for symbols the lookup misses |
| `data/market.sqlite` | saved candles, instrument IDs and alerts (`--db` for another file, `--no-db` to disable) |
| `nse_holidays.csv` | NSE holidays and special sessions; add next year's in December |
| `logs/screener.jsonl` | JSON log, rotated (see docs/OPERATIONS.md) |
| `data/scrip_master.csv` | Dhan's instrument list, cached and re-downloaded once a day |

**Most-used `config.py` settings.** Most of these can also be changed while the app runs on the
[Settings page](#settings-page) (`/settings`), which saves them to `data/settings.json`. If you edit
`config.py` itself, restart the app:

| Setting | Default | Change it to… |
|---|---|---|
| `UNIVERSE` | `"watchlist"` | `"nse"` to scan every NSE stock by default |
| `PORT` | 8050 | run on another port (or use `--port`) |
| `MIN_AVG_TURNOVER_CR`, `MIN_ATR_PCT` | 0 (off), 1.5 | a liquidity filter (e.g. 100 = ₹100 crore a day; 0 = none) and the volatility filter |
| `TABLE_CHG_PCT_MIN`, `TABLE_CHG_PCT_MAX` | none | list only stocks whose change % today is in this band, e.g. 0.3 to 1 (the dashboard's **Chg %** filter; tables only) |
| `MIN_PRICE`, `MAX_PRICE` | 0, 0 (off) | screen only stocks in a price range, e.g. `MAX_PRICE = 1000` (previous close; also on the Settings page) |
| `STRATEGIES` | see [Strategies](#strategies-and-the-setup-score) | change a strategy's thresholds, switch it off, or make a gate optional when its data is missing |
| `SCORE_WEIGHTS`, `SCORE_BOUNDS` | 20/20/15/15/10/10/5/5 | reweight the setup score, or change where a component scores full points |
| `CANDIDATE_GRACE_MINUTES` | 10 | how long a candidate may stop qualifying before it's removed |
| `ALERT_RULES` | see [Alert centre](#alert-centre) | switch an alert type off, or change its severity, threshold, scope or cooldown |
| `MIN_DIRECTIONAL_VOTES` | 4 (of 7) | 5 for stricter bullish/bearish lists |
| `EMA_STACK_VOTE`, `DAILY_EMA_STACK_VOTE` | True, True | `False` drops that EMA vote (each one removed lowers the maximum score of 9 by 1) |
| `RVOL_ALERT_THRESHOLD` | 2.5 | how big a volume spike must be to alert |
| `RVOL_ALERT_MIN_BAR_VALUE` | ₹25,00,000 | ignore spikes in stocks trading less than this per minute |
| `DEEP_SCAN_MAX` | 500 | fewer stocks in the full scan (fewer API calls) in all-NSE mode |
| `DATA_API_MAX_PER_SECOND` | 4 | stay below Dhan's limit of 5 requests a second |

Every formula and threshold is explained in [technical_indicators_calculations.md](technical_indicators_calculations.md).

**`app.py` options:**

| Option | Meaning |
|---|---|
| `--universe watchlist\|nse` | what to scan (default from `UNIVERSE`) |
| `--feed auto\|rest\|ws` | live data source (`auto`: WebSocket for `nse`, REST for the watchlist) |
| `--watchlist FILE` | another symbol list |
| `--demo` | simulated data, no account needed |
| `--demo-speed N`, `--demo-start HH:MM`, `--demo-universe-size N` | demo clock speed, start time, size of a simulated all-NSE universe |
| `--port N` | dashboard port (default 8050) |
| `--db FILE`, `--no-db` | another database file, or none |
| `--once` | one REST scan printed to the terminal (watchlist mode only) |
| `--no-browser` | don't open the browser automatically |
| `-v` | verbose logging |

**API usage:**

- **Watchlist (REST) mode:** while the market is open, two small candle requests per stock every
  60 s (5-min candles for the indicators, 1-min for Bar RVOL, each fetching only new candles), plus
  one price request for all stocks every 5 s. A refresh of 50 stocks takes about 25 s.
- **Throttling:** data requests are limited to 4 a second and quote requests to 1 a second.
- **Market closed:** it polls every 5 minutes.
- **All-NSE mode:** see [Scanning every NSE stock](#scanning-every-nse-stock-websocket-feed).

### 10. Check these on your first live run

- The header shows how many watchlist symbols resolved. With the default `watchlist.csv` expect about
  **3,213 of 3,226**: the 13 without a Dhan ID (suspended stocks such as `EDUCOMP`) are listed under
  *Data errors*. With `nifty50.txt` it's **50/50** (as of 24-Nov-2026, including `TMPV`). Any other
  missing symbol is listed there with the reason.
  - Only NSE equity shares are accepted. BSE-only listings, ETFs, indices, bonds and F&O contracts are skipped.
  - `BE`/`BZ` (trade-for-trade) and SME stocks are accepted, but tagged and kept out of the bullish/bearish lists.
  - Fix the spelling in your watchlist file, or add a row to `security_ids_override.csv`:
  ```csv
  symbol,security_id
  TMPV,12345
  ```
- The header benchmark says **NIFTY 50**, not *Watchlist median*. The default (`IDX_I` / `13`)
  matches the NIFTY row in Dhan's instrument list (checked 24-Nov-2026). If the index feed fails,
  the screener falls back to the median move of the stocks it scans.
- Hover over a row: the *last bar* time should run from 09:15 to 15:25 IST on 5-minute bars.
- In `--feed ws` or `--universe nse` mode, the header badge reads **WebSocket live** and shows a
  rising packets-per-second count.

### 11. Troubleshooting

| What you see | What to do |
|---|---|
| `DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN must be set` | `.env` is missing or empty (step 4), or run `--demo` |
| Red banner: *credentials rejected* / *access token expired* | generate a new token and put it in `.env`; the running app loads it within a minute |
| Red banner: *access-token renewal failed* | it retries every 5 min until the token expires; check the network, or put a fresh token in `.env` |
| Error `806` / *Data APIs not subscribed* | your Data API subscription isn't active |
| `Address already in use` | the app is already running, or pick another port: `--port 8060` |
| Symbols listed under *Data errors* | fix the spelling or add an override (step 9) |
| `NotOpenSSLWarning … LibreSSL` at startup | harmless warning from macOS's built-in Python; ignore it |
| Browser didn't open | open <http://127.0.0.1:8050> yourself |
| No sound or notifications | click **Enable sound & notifications** (needed again after each page load), check *Alert settings*, and allow browser notifications in your system settings |
| All-NSE banner *Loading daily history* for a long time | normal on a first run (~14 min); run the pre-market backfill next time |
| *Live feed disconnected — reconnecting* | it retries on its own (up to every 30 s). Check your internet if it lasts. |
| Empty or stale tables on a weekday | probably an exchange holiday (not detected); it shows the last session |
| `Scanning every NSE stock needs the WebSocket feed` | don't combine `--universe nse` with `--feed rest` |

## Stock universe and sectors (`watchlist.csv`)

`universe.py build` produces one row per NSE-listed stock. These are the columns and where each comes from:

| Column | Source |
|---|---|
| `symbol`, `company`, `series`, `isin`, `listing_date` | NSE's official equity lists: main board [`EQUITY_L.csv`](https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv) and SME [`SME_EQUITY_L.csv`](https://nsearchives.nseindia.com/emerge/corporates/content/SME_EQUITY_L.csv). Rights-entitlement lines (`-RE1`) are dropped. |
| `board` | `MAIN` or `SME`, from which file the stock came from |
| `dhan_security_id` | Dhan's public instrument list (empty when Dhan doesn't list the stock, e.g. suspended) |
| `nse_sector` | NSE Indices' [Nifty Total Market constituents](https://www.niftyindices.com/IndexConstituent/ind_niftytotalmarket_list.csv) (*Industry* column, NSE's 22 sectors), for ~750 stocks |
| `dhan_sector`, `dhan_industry` | Dhan's **Company Info API** (`POST /v2/data/companyinfo`, `metrics: ["CO"]`, Data API plan). One request per stock, cached in `data/market.sqlite` and refreshed after 30 days. |
| **`sector`** | the sector the scanner uses, always in **NSE's 22-sector names** (rules below) |
| `sector_source`, `sector_confidence` | which rule produced `sector`, and how strong the evidence was |
| `as_of` | build date |

**How `sector` is chosen:**

1. **`NSE index file`:** NSE's own sector, when the stock is in the Nifty Total Market index.
2. **`derived from Dhan industry`:** otherwise, Dhan's industry is translated to an NSE sector
   **only if** the stocks that have both labels agree strongly (at least 75% of at least 3 stocks).
   `sector_confidence` is that share.
3. **`derived from Dhan sector`:** the same rule on Dhan's broader sector, when the industry rule fails.
4. **`Dhan sector (same name as NSE)`:** when there's too little overlap to measure agreement, but
   Dhan's sector is spelled exactly like one of NSE's sector names (e.g. *Forest Materials*), that
   name is used. No confidence value is given.
5. **`unmapped`:** none of the above. The raw Dhan values stay in their own columns, and nothing is guessed.

Change the thresholds with `--min-agreement` and `--min-overlap`. Derived sectors show in *italics* in
the dashboard's Sector column, and hovering shows the source.

**What isn't used:** NSE's website shows sectors on each stock's quote page, but only through an API
behind bot protection, and BSE's company list blocks automated downloads (HTTP 403). Neither is
scraped. NSE reorganises its indices twice a year, so rebuild with `--refresh` after a rebalance.

## Sector strength and market breadth

The **Sector strength** panel ranks every sector by how it's doing against NIFTY 50. Two new stock
columns show the same view per stock:
- **Sector RS:** the stock's sector vs Nifty.
- **vs Sector:** the stock vs its own sector, with **P** = its percentile among sector peers (P100 = best).

Hover for the 5, 15, 30 and 60-minute and day values. The formulas are in
[technical_indicators_calculations.md §12](technical_indicators_calculations.md#12-change--gap--and-relative-strength).

- **How a sector is measured:** 10 sectors have their own NSE sector index (NIFTY AUTO, FIN SERVICE,
  FMCG, IT, MEDIA, METAL, HEALTHCARE, REALTY, CONSR DURBL, OIL AND GAS). These stream live like NIFTY 50
  (WebSocket ticker, or the REST quote call), with 1-min candles fetched every minute for the 5–60-minute
  returns. The other 12 sectors have no matching index, so they use the **median of the sector's liquid
  EQ stocks**, labelled "median of N stocks". Edit `sector_indices.csv` to change the mapping.
- **Same start time for everything:** every 5–60-minute return starts from the same point on a 5-minute
  grid, and a missing value shows "—", never zero.
- **States:** "outperforming, falling less than Nifty" is shown separately from "outperforming, rising".
- **Breadth** (header badge, hover for more): advancing vs declining, % above VWAP, above the 5-min EMA20,
  at the day high or low, and Day RVOL above 2.5×.
- **Data state:** a red **DATA STALE** badge and banner appear when prices stop updating for 30 s
  while the market is open (`STALE_FEED_SECONDS`). **FEED DISCONNECTED** shows while the WebSocket reconnects.
- **VWAP %** is now the distance from VWAP (the VWAP price is in the tooltip). **OR** shows ▲ above,
  ▼ below or ↔ inside the opening range.

## Momentum and volume detail (RSI, MACD states, VWAP events, RVOL)

All momentum and volume indicators use **completed candles only**, so values don't flicker while a
candle forms. An indicator without enough candles shows **—** (hover to see how many it needs) and gets
no vote. The formulas and worked examples are in
[technical_indicators_calculations.md](technical_indicators_calculations.md) (§4, §8, §9, §14, §17).

| Column | What's new |
|---|---|
| **Symbol** | breakout tags moved here: the latest level crossed, plus "+N" for the others (hover for the list) |
| **VWAP %** | ↗/↘ = VWAP's slope over 15 min. **reclaim** / **lost** = price crossed VWAP by more than 0.1% and held there 60 s (hover for the time) |
| **EMA 5m** | tooltip lists the alignment checks: EMA9 > EMA20, EMA20 > EMA50, price > EMA20, EMA20 rising |
| **MACD** | histogram + state arrow: ▲ strong bullish, ↑ bullish but weakening, ↗ early bullish reversal, ↓ bearish. The 15-min MACD state is in the tooltip |
| **RSI** | Wilder's RSI(14), ↗/↘ over 3 candles; ≥ 70 and ≤ 30 highlighted. Context only |
| **DMI · ADX** | +DI/−DI · ADX in one cell, ↗/↘ = ADX rising or falling |
| **RVOL day · 5m** | Day RVOL (cumulative to the same time of day) and **5m RVOL** (the last 5-min candle vs the same time slot on the previous 20 sessions). Both need at least 10 past sessions; the tooltip shows the baseline and session count |
| row tooltip | adds pivots/CPR, intraday ATR %, Bollinger bandwidth and squeeze, OBV trend and MFI |

Each completed 5-min evaluation is also saved to the `indicator_snapshots` table in
`data/market.sqlite` (turn off with `SAVE_INDICATOR_SNAPSHOTS = False`), so you can review how a
stock's RSI, MACD state, ADX and RVOL evolved after the session:

```bash
sqlite3 -header -column data/market.sqlite \
  "SELECT bar_ts, rsi, macd_state, adx, rvol, candle_rvol FROM indicator_snapshots
   WHERE symbol = 'INFY' AND session_day = '2026-11-24' ORDER BY bar_ts"
```

## History database (SQLite)

In live mode every candle the dashboard fetches is saved to `data/market.sqlite`, along with
instrument IDs and every breakout alert. The database works as a cache. Each refresh asks
Dhan only for bars that aren't stored yet, starting from the last saved bar, which is re-fetched
in case it was still forming. Restarting mid-day restores that day's alert log, so levels that
already fired stay quiet. Demo mode keeps its data in memory, so simulated prices never mix with
real history. `--no-db` turns the database off.

Build a longer history with `history.py`:

```bash
.venv/bin/python history.py backfill --watchlist nifty50.txt   # 1 year of 5-min + 5 years of daily: NIFTY 50 stocks, NIFTY 50, sector indices
.venv/bin/python history.py backfill --watchlist nifty50.txt --days 730 --interval 5 15
.venv/bin/python history.py status                         # rows and date range per symbol
.venv/bin/python history.py export RELIANCE --interval 5 --from 2026-01-01 --out reliance_5m.csv
.venv/bin/python history.py export "NIFTY 50" --interval D
.venv/bin/python history.py alerts --days 10               # alerts the dashboard saved
```

- **Incremental and resumable.** Re-running a backfill downloads only what's missing. If it stops
  part-way (network error, expired token), run it again to continue. The `coverage` table records
  the ranges already requested, so weekends and holidays aren't requested again.
- **Cost.** Dhan serves at most 90 days of intraday data per request and about 5 years in total.
  One year of 5-min plus five years of daily candles for the 61 series of `--watchlist nifty50.txt` (50 stocks,
  NIFTY 50, 10 sector indices) is about 430 requests (around 2 minutes at 4 requests a second) and about 90 MB.
  **Without `--watchlist`, backfill uses `watchlist.csv`: all ~3,200 stocks, about 22,000 requests (1.5 hours)**,
  so pass a watchlist unless you mean it. 1-min candles are 5× larger, so keep long backfills at 5-min.
- **Timing.** Run backfills outside market hours, or before starting the dashboard, so the two
  don't compete for Dhan's rate limit. The database itself handles both running at once.
- **Explore with SQL.** macOS includes `sqlite3`, and the `v_candles` view adds the symbol to each candle:
  ```bash
  sqlite3 -header -column data/market.sqlite \
    "SELECT ts, open, high, low, close, volume FROM v_candles
     WHERE symbol = 'RELIANCE' AND interval = 'D' ORDER BY ts DESC LIMIT 10"
  ```
  Tables: `candles` (interval `'D'` = daily, `'5'` = 5-min; timestamps in IST; intraday `ts` is the
  bar's start time), `instruments`, `coverage`, `alerts`, `indicator_snapshots`, `scanner_candidates`,
  `candidate_events`, `scanner_evaluations`, `strategy_config`, `backtest_runs`, `backtest_trades`,
  `company_info`, `meta`.

## Dashboard

| Part | What it does |
|---|---|
| **Header** | market phase and IST clock, NIFTY 50 level and change, breadth, *live push* / *polling* badge, Dhan feed state and last tick (live-feed mode), DATA STALE / FEED DISCONNECTED, 🔔 unread alerts |
| **Sector strength** | each sector's index value (where it has one), day %, RS vs NIFTY 50 for 5/15/30/60 min and the day, state and rank. Sortable |
| **Filter bar** | sector, strategy, minimum setup score, minimum Day RVOL, **Chg % from … to …** (change today vs the previous close), direction (vote bias). Applies to the candidate and vote tables. The Chg % band is checked on the server across every stock before the tables are cut to their row limit, and saved as a setting (`TABLE_CHG_PCT_MIN` / `MAX`); the others are remembered in this browser and filter the rows already on the page |
| **Tabs** | All candidates · Strong Momentum · Early Breakout · Sector Leaders · Bullish / Bearish (votes) · Alert history |
| **Tables** | click a header to sort (again to reverse, a third time for the default order); rows are updated in place, NEW / weakening / removed candidates are marked, rows with a fresh alert are highlighted |
| **Stock detail** (click a row) | 1m / 5m / 15m candles with VWAP, EMA 9 / 20, opening range and PDH / PDL lines, volume, MACD, RSI and ADX panels with a crosshair readout; quote; every strategy gate and score component; stock vs sector vs NIFTY returns per horizon; RVOL with its baseline; indicators; levels; today's alerts for the stock; data quality. Link to it with `#stock=SYMBOL` |
| **Alert centre** | below the tables and as the *Alert history* tab (see [Alert centre](#alert-centre)) |
| **Settings** (`/settings`) | see below |
| **Backtests** (`/backtests`) | saved `backtest.py` runs: variants in-sample vs out-of-sample, equity curve, breakdowns, trades (see [Backtesting](#backtesting)) |

The chart's indicators are calculated on the server from completed candles (the browser only draws
them), so the chart and the tables never disagree. In live-feed mode the table's VWAP is the
exchange's; the chart's VWAP comes from the candles, and the panel says so.

### Settings page

<http://127.0.0.1:8050/settings> lists about 140 settings in eight groups: universe and session,
each strategy's gates (including on/off and *skip when data is missing*), setup-score weights and
bounds, hard filters and votes, indicator periods and timeframes, candidate lifecycle, alert types
(on/off, severity, scope, cooldown) and refresh intervals.

- All changes are **validated together** (types, ranges, and checks like MACD fast < slow), then applied
  to the running screener from its next refresh. Settings marked **restart** (universe, candle size,
  Dhan request rate) take effect when you restart `app.py`.
- They're saved to `data/settings.json` (the demo uses `data/settings.demo.json`, so experiments there
  never change the live screener) and loaded over the `config.py` defaults at start. Each field shows its
  default and has *Reset*. Delete the file to go back to `config.py` entirely.
- Dhan credentials are not settings: they stay in `.env` and never reach the browser.
- Changing strategy or score settings starts a new `config_id`, which every saved evaluation, candidate
  and alert records, so you can tell later which rules produced them.

### Local-only safety

The server listens on `127.0.0.1`. It also rejects requests whose `Host` isn't local (a guard against
DNS-rebinding pages), accepts changes (`POST`) only as JSON (other websites can't send that through
your browser without a permission check this server never grants), and accepts the `/ws` connection
only from its own page. `GET /health` returns `ok` or `degraded` (HTTP 503) for scripts.

**API** (all JSON): `/api/state`, `/api/search?q=`, `/api/stock/SYMBOL`, `/api/chart/SYMBOL?interval=1|5|15`,
`/api/explain/SYMBOL`, `/api/alerts?…`, `POST /api/alerts/ack`, `/api/settings`, `POST /api/settings`,
`/health`, and the WebSocket `/ws`.

## Strategies and the setup score

After the hard filters (ATR ≥ 1.5%, EQ series, the optional turnover minimum `MIN_AVG_TURNOVER_CR`, the optional price band `MIN_PRICE` /
`MAX_PRICE`), every stock
with intraday indicators is checked against three independent strategies from the spec. Each has its own
settings in `config.STRATEGIES` and its own reason codes, and a stock can qualify for several:

| Tab | Gates (all must pass; defaults) |
|---|---|
| **Strong Momentum** (MOM) | Day RVOL > 2.5× · sector beating NIFTY 50 · stock beating its sector · MACD line > 0 and histogram > 0 · confirmed above VWAP · RSI > 50 · ADX > 20 and +DI > −DI (optional confirmation) |
| **Early Breakout** (BRK) | Day RVOL > 2.0× · first 5-min close above the opening-range high, PDH or a tight consolidation high within the last 30 min, price still above it · 5m RVOL > 2.0× on the break or latest candle · RSI > 50 or rising · MACD bullish cross or rising histogram · ADX rising |
| **Sector Leaders** (LDR) | sector beating NIFTY 50 · stock beating its sector · top 10% of its sector · Day RVOL > 2.5× · confirmed above VWAP · EMA9 > EMA20 |

- **All candidates** lists every stock that qualifies for at least one strategy, ranked by the
  **setup score** (0–100): cumulative RVOL 20, stock vs sector 20, sector vs Nifty 15, MACD 15, VWAP
  position and slope 10, ADX and direction 10, RSI 5, EMA alignment 5. Each part is a capped linear
  scale; parts without data drop out and the score is points ÷ the points that could be scored (shown as
  "/85"). Labels: Strong ≥ 80, Good ≥ 65, Watchlist ≥ 50, Low below; "data?" when fewer than 70 points
  could be scored; those rank after the fully scored ones. **It ranks setups; it is not a probability of profit.**
- **Missing data:** a gate without data (e.g. no sector RS for a sector with too few stocks) **blocks**
  the strategy and shows as "?". To make a gate optional, add its code to the strategy's `skip_if_missing`.
- **Lifecycle:** a candidate is **NEW** for 10 minutes, then active; **weakening** when it stops
  qualifying; **removed** after 10 more minutes (greyed for 30). A change must hold 30 s to count, so a
  price flickering around a threshold doesn't flip it. Every change appears under *Candidate changes*.
- **Why / why not:** click any symbol (in any table or the search results) to see every gate with its
  value, every score component with its points, and that stock's lifecycle today.
- **Saved:** `scanner_candidates` (state, restored after a restart), `candidate_events` (audit trail),
  `scanner_evaluations` (score and every strategy's result per stock per completed candle) and
  `strategy_config` (the settings each row was evaluated with), pruned after 60 days.

```bash
sqlite3 -header -column data/market.sqlite \
  "SELECT ts, strategy, symbol, event, score, reasons FROM candidate_events
   WHERE session_day = '2026-11-24' ORDER BY id"
```

The formulas, the break detection and a worked score example are in
[technical_indicators_calculations.md §19](technical_indicators_calculations.md#19-strategies-setup-score-and-candidate-lifecycle).
The vote-based lists below are still on the **Bullish / Bearish (votes)** tab.

## How a stock is classified

Every formula, with worked examples, is in
[technical_indicators_calculations.md](technical_indicators_calculations.md).

Seven **directional votes** (the coloured dots and the EMA stack tags): five on 5-minute candles,
the opening range, and the daily EMA stack:

| Vote | Bullish | Bearish |
|---|---|---|
| VWAP | price above session VWAP | price below |
| MACD (12, 26, 9) | MACD line above signal (histogram > 0) | below |
| DMI (14) | +DI above −DI | −DI above +DI |
| Relative strength | stock % change ≥ benchmark + 0.2 | ≤ benchmark − 0.2 |
| Opening range (first 15 min) | price above OR high | price below OR low |
| EMA stack, 5-min (9, 20, 50, 200) | price > EMA9 > EMA20 > EMA50 > EMA200 | price < EMA9 < EMA20 < EMA50 < EMA200 |
| EMA stack, daily (9, 20, 50, 200) | same order on the daily EMAs (today's value moves with the live price) | the reverse |

When an EMA set is out of order (mixed) it gives no vote. The daily set also shows a 50×200
golden/death-cross tag, for information. At least **4 of 7** votes on one side, and more than the other
side, puts the stock in that list. Set `MIN_DIRECTIONAL_VOTES = 5` for a stricter list. To drop an EMA
vote, set `EMA_STACK_VOTE = False` or `DAILY_EMA_STACK_VOTE = False`. The **vote score (out of 9)** is the
number of votes plus two strength checks: **ADX ≥ 20** (a real trend, not chop) and
**Day RVOL ≥ 1.5×** (more volume so far today than usual at this time of day). Each list is ranked by
score, then ADX, and the top 5 are marked **TOP**.

Stocks go to *Neutral & filtered* when their votes are mixed, or when they fail the
tradability filters: 14-day ATR below 1.5% of price, 20-day average turnover below `MIN_AVG_TURNOVER_CR` (off by default),
or when they're outside the optional price band (`MIN_PRICE` / `MAX_PRICE`, judged on the previous
close). Stocks outside the band also get no volume-spike or breakout alerts.

## Alert centre

All alerts, of every type, are created on the server when something **changes** and saved in
`data/market.sqlite`. Nothing fires because you refreshed the page, and a restart neither loses nor
repeats an alert.

| Severity | What | Examples |
|---|---|---|
| **CRITICAL** | the data can't be trusted | feed disconnected or prices stale for 10 s, credentials rejected, the app's own loop failing |
| **HIGH** | strong setups | a stock qualifies for Strong Momentum or Early Breakout, setup score crosses 80, a confirmed breakout |
| **MEDIUM** | indicator events | Sector Leaders qualifies, a candidate loses eligibility, Day RVOL crosses 2.5×, stock or sector RS turns positive, top 10% of the sector, MACD bullish cross, VWAP reclaim/loss, unconfirmed breakout, ⚡ volume spike, data failures |
| **LOW** | information | candidate removed after the grace period, requalified, feed recovered |

- **Why it fired:** each alert's message gives the reason and the values (RVOL, stock vs sector, sector
  RS, MACD, RSI, VWAP distance, setup score…); hover it for the full snapshot taken at that moment.
- **Noise control:** indicator alerts cover only candidates and stocks with a setup score of at least 50,
  fire on a rising edge with a re-arm level (e.g. RVOL must fall below 2.0× before a second 2.5× alert),
  and have a per-stock cooldown. Repeated data failures fold into one alert with a count.
- **Alert centre panel:** filter by severity, setups vs system, strategy, symbol and date (older days come
  from the database), show only unacknowledged ones, and acknowledge one or all shown. The 🔔 header badge
  counts unread CRITICAL and HIGH alerts. System alerts are grey with a SYSTEM tag.
- **Toasts, sound, notifications:** chosen per alert type in *Alert settings* (stored in this browser).
  Sound is off until you switch it on. Notifications need your permission and are sent while the tab is
  in the background.
- **From the terminal:** `.venv/bin/python history.py alerts --days 1 --severity HIGH CRITICAL --unacked`

Every rule, threshold and example is in
[technical_indicators_calculations.md §22](technical_indicators_calculations.md#22-alert-centre-rules-severity-deduplication);
change them in `config.ALERT_RULES`. **Alerts are informational: they are not buy or sell instructions.**

## How breakouts work

A breakout fires **once per stock per level per day**, the moment the live price
crosses the level by more than 0.05%. Levels: **OR High/Low** (only after 09:30),
**PDH/PDL** (previous day's high and low) and **52W High/Low**.

- Stocks that are already past a level when you start the app don't alert. Only a fresh cross alerts.
- Each alert shows five checks: MACD, DMI, ADX, RVOL and VWAP agreeing with its direction.
  **Confirmed** means at least 4 of 5 agree; confirmed breakouts are HIGH severity, others MEDIUM.
  Tick *Breakouts: only confirmed ones* in *Alert settings* to cut the noise.
- When the tab is in the background you also get a desktop notification and a count in the tab title.

## Scanning every NSE stock (WebSocket feed)

`app.py --universe nse` takes every **NSE equity share** from Dhan's instrument list and subscribes
them all on **one Dhan WebSocket connection**, in Quote mode. Dhan allows 5,000 instruments per
connection and 100 per subscribe message.

An NSE stock here means: exchange NSE, cash segment, instrument type **ES (equity share)**, and a
stock series. In Dhan's list on 24-Nov-2026 that gives **3,213 stocks**:

| Series | Stocks | What it means | What you get |
|---|---|---|---|
| EQ | 2,419 | normal, intraday allowed | everything |
| BE / BZ | 180 / 32 | trade-for-trade, delivery only | live data and ⚡ alerts, tagged `BE`/`BZ`, listed under *filtered* |
| SM / ST / SZ | 479 / 102 / 1 | SME platform, traded in lots | live data and ⚡ alerts, tagged, listed under *filtered* |

The same list also has 361 ETFs and 22 NSE test instruments in the EQ series, plus thousands of
bonds and G-secs. These are excluded. To change which series count as stocks, or which get the full
treatment, edit `NSE_STOCK_SERIES` and `INTRADAY_SERIES` in `config.py`.

| What | Which stocks | Where it comes from |
|---|---|---|
| Price, day volume, day open/high/low, VWAP, % change, relative strength | **all** (3,213) | WebSocket Quote packets (VWAP = Dhan's average trade price), prev close packet |
| 1-min candles → **Bar RVOL ⚡ alerts** | **all** | built from the ticks (as in the reference project) |
| PDH/PDL, 52-week levels, ATR, turnover (the filters) | **all** | daily candles over REST, loaded in the background, cached in SQLite |
| MACD, +DI/−DI, ADX, Day RVOL, opening range → **bullish/bearish lists, breakout alerts** | the **500 most liquid stocks that pass the filters** (`DEEP_SCAN_MAX`) | 5-min candles over REST, fetched right after each 5-min candle closes (+5 s): strategy candidates first, then the stocks with the biggest 1-min volume bursts in that candle (from the feed), then by turnover. A whole pass takes about 2 minutes at Dhan's rate |

Why not the full indicator scan for all 3,213? That would need about 3,200 REST requests every
5 minutes (more than 13 minutes per pass at Dhan's rate). Stocks that fail the turnover and ATR
filters (or aren't EQ series) would land in *Neutral & filtered* anyway. They still get live prices and ⚡ spike alerts.

- **Daily history.** The first run downloads 990 days of daily candles for every stock, enough
  for a settled daily EMA200 (about 3,200 requests, roughly 13–14 minutes; a database from an
  earlier version tops up the older days once). Each morning it then tops up yesterday's candle for
  each stock (also about 3,200 small requests). Your watchlist loads first, then EQ stocks, then BE/SME. Either run
  `history.py backfill --universe nse --daily-only` before 09:00, or start the app by about 08:45.
  Stocks join the lists as their history loads; the banner shows progress.
- **Keep the Mac awake.** A sleeping Mac freezes the whole screener: no prices, no candles, no
  alerts. In live mode the app runs macOS `caffeinate -i` while it's open (`PREVENT_SLEEP`), so the Mac
  doesn't idle-sleep; the display can still turn off. Closing the lid on battery still sleeps it. After
  any pause the app raises a HIGH *Computer asleep* alert with the times, shows a banner and reconnects
  the feed at once. If the 5-min refresh makes no progress for 2 minutes while the market is open
  (network or Dhan trouble), you get a CRITICAL *Indicator refresh* alert and an INDICATORS BEHIND
  badge; every Dhan request gives up after 30 s (`API_REQUEST_DEADLINE_SECONDS`), DNS lookups included.
- **Bar RVOL needs 20 live candles**, so spike alerts begin about 21 minutes after the feed connects.
  Start the app before 09:15 to have them from 09:35. The candle the feed connected in is ignored
  because it's partial. After a reconnect, volume traded while disconnected is not dumped into one
  candle, so a reconnect never causes a fake spike.
- **Noise control.** A ⚡ alert needs the spike candle to trade at least ₹25 lakh
  (`RVOL_ALERT_MIN_BAR_VALUE`), which filters out thinly traded stocks. Breakout alerts only fire for
  stocks that pass the filters (`BREAKOUT_ALERTS_REQUIRE_FILTERS`). With this many stocks, ticking
  *Breakouts: only confirmed ones* in *Alert settings* is recommended.
- **Dashboard.** Each table shows the top 100 (`DASHBOARD_MAX_ROWS`) with the full count. Use the
  search box to find any stock.
- **Daily API usage.** About 3,200 daily requests plus 500 stocks × 75 five-minute bars ≈ 41,000 requests a day. Check
  this against your Dhan plan's daily Data API limit, and lower `DEEP_SCAN_MAX` if needed. The WebSocket itself
  isn't counted per request.
- **Feed health.** The header badge shows the connection, how many instruments are streaming, and
  packets per second. The feed reconnects automatically, backing off up to 30 s. A "token expired"
  or "not subscribed" disconnect stops it and shows a red banner.

## Volume-spike alerts (Bar RVOL)

The method comes from the reference `intraday_scanner` project (`src/engines/rvol_engine.py`):

```
Bar RVOL = volume of the latest completed 1-min candle
           / average volume of the 20 completed candles before it
```

- Only **completed** candles count: a candle still forming is never compared.
- The bands match the reference: LOW below 0.8, NORMAL below 1.2, HIGH below 2.0, VERY HIGH 2.0 or more.
  The *Bar RVOL* column marks VERY HIGH values and shows ⚡ above 2.5×.
- An **alert fires when Bar RVOL is greater than 2.5×**. It goes to the alert centre and the database,
  and pops up as a toast (unless you switch that type off in *Alert settings*). Green or red on the
  alert is the spike candle's own direction.
- Every candle completed since the previous refresh is checked, so a slow refresh can't skip a spike.
- **Start time.** The 20-candle look-back runs into the previous session (as in the reference).
  So before 09:35, each candle is compared with yesterday's closing minutes, and almost any busy
  day looks like a spike at the open. By default (`RVOL_ALERT_FROM = "auto"`) alerts therefore start
  from the 09:35 candle, the first whose average is entirely from today. Set `"09:15"` to alert
  from the first candle.
- **No repeats.** At most one alert per stock every 10 minutes. Candles that finished more than
  5 minutes ago don't alert, for example when you start the app mid-session.
- **Delay.** Candles are fetched every 60 s, so an alert arrives about 1–1.5 minutes after the
  spike candle closes.
- **Three different RVOLs.** *Day RVOL* (today's total volume vs the same time on the previous 20
  sessions) still drives the score. *5m RVOL* compares the last 5-min candle with the same time slot on
  those sessions. *Bar RVOL* catches sudden bursts, such as block deals or news.

Settings: `RVOL_ALERT_THRESHOLD`, `RVOL_SPIKE_INTERVAL_MIN`, `RVOL_SPIKE_LOOKBACK_BARS`,
`RVOL_ALERT_FROM`, `RVOL_ALERT_COOLDOWN_MIN`, `RVOL_ALERT_MAX_AGE_MIN` and `RVOL_SPIKE_ENABLED`
in `config.py`.

## Backtesting

`backtest.py` replays saved history through the **same** indicator, relative-strength, filter,
strategy and score code as the dashboard, one 5-minute candle close at a time, and simulates a long
trade whenever a strategy starts qualifying. **It is a simulation on past data with your
assumptions, not a prediction, and it never places orders.**

```bash
.venv/bin/python backtest.py --demo                        # simulated history: checks the tool, means nothing about markets
.venv/bin/python backtest.py                               # your SQLite history, NIFTY 50 stocks, no network
.venv/bin/python backtest.py --from 2026-09-01 --to 2026-11-23 --strategies early_breakout
.venv/bin/python backtest.py --stop-atr 2 --target-r 2 --exit-on-lost 2 --slippage-bps 5
.venv/bin/python backtest.py --variant "RVOL 2.0: STRATEGIES.strong_momentum.cum_rvol_min=2.0; STRATEGIES.sector_leaders.cum_rvol_min=2.0"
```

**What you need first.** At least 11 sessions of 5-min candles for the stocks, NIFTY 50 and the NSE
sector indices (RVOL compares with 10 earlier sessions; sector RS needs the index candles). Your
database starts with what the dashboard has cached. For a meaningful test, fetch more once, outside
market hours: `.venv/bin/python history.py backfill --watchlist nifty50.txt --days 180` (the NIFTY 50 stocks, NIFTY 50 and the 10 sector
indices; about 300 Dhan requests, around 2 minutes). Without index candles, Strong Momentum and Sector Leaders
can't qualify, and the report says so. `--fetch` makes `backtest.py` fill gaps from Dhan itself.

**How a trade is simulated**

- **Signal:** a strategy starts qualifying at a candle's close (decided from candles up to that close only).
- **Entry:** the next candle's open plus slippage. No entries after 14:45; one trade per strategy, stock and day.
- **Exits (user-defined examples, not recommendations):** stop at 1.5 × the 5-min ATR, optional target
  (`--target-r`), optional exit after N closes without qualifying (`--exit-on-lost`), square-off at 15:15.
  Candle highs and lows decide stops and targets; if both are touched in one candle, the stop counts.
- **Costs:** brokerage (₹20 or 0.03% per order), STT, exchange and SEBI charges, stamp duty and GST,
  plus 3 bps slippage per side, all in `config.BACKTEST["costs"]`. These approximate Indian intraday
  equity charges at the time of writing: **check your broker's current charge sheet** and set your own.
- **Size:** ₹1,00,000 per trade (`--capital`).

**What you get** (printed, saved as `data/backtests/<id>.md` / `.json`, in the `backtest_runs` /
`backtest_trades` tables, and on the dashboard's **Backtests** page, <http://127.0.0.1:8050/backtests>):
trades, win rate, average net return with a 95% interval, expectancy in ₹, profit factor, maximum
drawdown, average MFE / MAE (how far trades went for and against you), exit reasons, and breakdowns by
time of day, setup score, sector and exit. Each is shown **in-sample** (the first 70% of sessions) and
**out-of-sample** (the rest), for every variant.

**How to read it**

- Pick settings using the **in-sample** sessions only, then judge them on the **out-of-sample** ones.
  A variant that wins in-sample but not out-of-sample (highlighted on the page) was probably fitted to noise.
- **Fewer than 30 trades** (⚠) are too few to tell skill from luck. If the 95% interval includes 0, the
  test hasn't shown an edge either way.
- Don't tune to the latest session, and remember costs: many small intraday edges disappear after them.

**Limitations:** decisions are made at candle closes (the live screener also reacts between closes, and
its 30-second candidate confirmation becomes "held at the close"); today's stock list and sector mapping
are used for the whole period (survivorship bias); 5-min candles can't show the order of moves inside a
candle; liquidity and partial fills aren't modelled.

## Tuning

Every threshold lives in `config.py`, including the candle interval, opening-range length,
MACD/DMI periods, filters, the vote and score rules, breakout buffer and polling rates.
Treat each value as a hypothesis to test, not a rule.

## Known limitations

- The WebSocket client is tested against a fake Dhan server (connect, subscribe batches,
  binary packets, reconnect, token-expiry disconnect) and its packet decoding matches the
  official `dhanhq` SDK, but it has not yet been run against the real Dhan feed.

- Exchange holidays come from `nse_holidays.csv`, which you update each December when NSE publishes
  the next year's list (the 2026 rows were cross-checked from two broker pages; verify them against NSE's circular).
- Breakouts trigger on price ticks, not candle closes, so some will reverse straight away
  (false breakouts). A good exercise is to add a candle-close confirmation option.
- Indicators ignore the still-forming candle, so they lag a live chart by up to one candle (by design).
- Day RVOL averages the previous 20 sessions (`RVOL_LOOKBACK_SESSIONS`). To have them, the screener
  loads 35 calendar days of 5-min candles per stock (`WARMUP_CALENDAR_DAYS`). That's one request per
  stock, and an existing database tops up the extra days once. With fewer than 10 past sessions (e.g. a
  recent listing) Day RVOL and 5m RVOL show "—".
- Demo-mode prices are random walks. They exercise the code but teach nothing about real markets.
- The strategies and setup-score weights are the spec's **initial** values. None of them has been
  backtested yet (step 7), so treat a qualified candidate as "worth a look", not as a signal.
- The strategies are long-side only, as in the spec. Bearish setups appear only on the votes tab.
- Strategies need intraday indicators, so in all-NSE mode only the deep-scan stocks (the 500 most liquid
  that pass the filters) can qualify.
- Alerts appear in the browser only while the dashboard is open; email / Telegram channels aren't built
  (the alert centre has a `sinks` hook for them).
- The alert thresholds, scopes and cooldowns are first guesses. Expect to tune them after a few live
  sessions (`ALERT_RULES`).

## Access-token auto-renewal

Dhan access tokens last 24 hours. Dhan's `RenewToken` call swaps a token that's **still valid** for a
new 24-hour one and expires the old one at once. The app makes that call itself
(`screener/token_manager.py`, live mode only) and switches to the new token without a restart: the REST
client, the live feed (it reconnects in about a second), log masking and `.env` all get it.

**When it renews** (IST, every day; change it on the Settings page under *Access token*, or in `config.py`):

| Rule | Setting | Default |
|---|---|---|
| At each of these times, if the token is at least an hour old | `TOKEN_RENEW_TIMES`, `TOKEN_RENEW_MIN_AGE_MINUTES` | 08:00 and 15:40; 60 min |
| When the app starts, if less than this is left | `TOKEN_RENEW_ON_START_HOURS_LEFT` | 12 hours |
| When the app stops (Ctrl+C), so the next start has about 24 hours | `TOKEN_RENEW_ON_EXIT` | on |
| At any time, when less than this is left (a missed schedule) | `TOKEN_RENEW_SAFETY_MINUTES` | 60 min |
| After a failure, until the token expires | `TOKEN_RENEW_RETRY_MINUTES` | every 5 min |
| All of the above | `TOKEN_AUTO_RENEW` | on |

A time missed while the computer slept is caught up when it wakes. Times before the app started aren't
(the startup rule covers those).

**What that means in practice**

- **App left running:** fully automatic.
- **Started every trading morning, stopped after the close:** automatic. The renewal when you stop it
  carries the token to the next morning, and the start renews it again.
- **A weekend or holiday with the app stopped** (or the computer off for more than a day): the token
  expires and Dhan won't renew an expired token. Generate a new one on web.dhan.co and put it in `.env`.

**Where to see it:** the header's **token … left** badge (hover for the plan and the last result), the
*Access token* panel on the Settings page (with **Renew now**), `/api/token`, `/health`, and alerts of
type *Access token*: LOW when it's renewed, CRITICAL when a renewal fails, the token expires, or the
new token couldn't be saved to `.env`.

**Things to know**

- Renewing expires the old token **everywhere**. A `history.py` or `backtest.py` still running with it
  fails; they read `.env` when they start, so run them again. Use one renewal mechanism only; a second
  one (a cron job, another copy of the app) would expire this app's token.
- `.env` is rewritten safely (temporary file, then swapped in) with mode 600; other lines are kept. The
  token is never logged or sent to the browser. An environment variable `DHAN_ACCESS_TOKEN` overrides
  `.env` when the app starts, so don't set one if you use auto-renewal.
- Dhan's docs say `RenewToken` works for tokens generated on Dhan Web. They don't show the reply format
  (the app accepts the likely field names) or say whether a renewed token can be renewed again (forum
  users say it can). The first real renewal confirms both: check the *Access token* alert.

## Operations, diagnostics and security

- **[docs/OPERATIONS.md](docs/OPERATIONS.md):** the daily routine, `manage.py check` diagnostics, the
  JSON logs (`logs/screener.jsonl`, with request and cycle ids, credentials redacted), the holiday calendar,
  backups and restore, the recovery playbook with its test plan, and troubleshooting.
- **[docs/SECURITY_REVIEW.md](docs/SECURITY_REVIEW.md):** what's protected and how (no order endpoints,
  credentials never logged or sent to the browser, local-only server, cross-site and clickjacking guards,
  content-security policy), with the findings: make `.env` private (`chmod 600 .env`) and move to a
  supported Python (3.9 is end-of-life).

```bash
.venv/bin/python manage.py check      # everything in place? token expiry, database, calendar, logs …
scripts/backup.sh                     # database + settings + data files -> backups/<timestamp>/
```

## Project layout

```
app.py                  FastAPI server (pages, JSON API, /ws push), CLI, starts the engine thread
manage.py               `check` (diagnostics) and `backup`
pyproject.toml          package metadata, dependencies, console scripts, pytest settings
scripts/                setup.sh, start.sh, demo.sh, backup.sh
nse_holidays.csv        NSE trading holidays and special sessions (update each December)
history.py              backfill / status / export / alerts for the SQLite history
backtest.py             replay saved history through the strategies (simulation only) and save a report
config.py               all thresholds and settings (+ .env loader)
universe.py             builds watchlist.csv: NSE lists + NSE sectors + Dhan IDs + Dhan Company Info
watchlist.csv           every NSE stock with its sector (generated; rebuild with universe.py build)
nifty50.txt             small symbol list (NIFTY 50) for REST mode and tests
screener/indicators.py  EMA, MACD + states, RSI, ATR, +DI/-DI/ADX, VWAP, opening range, RVOL, Bollinger, OBV, MFI, CPR
screener/engine.py      metrics, classification, breakout tracker, polling loop
screener/strategies.py  the three strategies: gates, reason codes, missing-data policy
screener/scoring.py     the 100-point setup score (bounded components, effective denominator)
screener/candidates.py  candidate lifecycle (new / active / weakening / removed) and its events
screener/alerts.py      alert centre: transition rules, severity, dedup / cooldown, feed monitor, persistence
screener/dhan_client.py read-only DhanHQ v2 client (instruments, candles, LTP, company info)
screener/universe.py    stock list + sector assignment (downloads, joins, Dhan→NSE sector crosswalk)
screener/relative_strength.py  horizon returns, sector RS vs Nifty, stock vs sector, breadth
sector_indices.csv      NSE sector -> NSE sector index (or composite), versioned
screener/demo_client.py simulated market with the same interface
screener/store.py       SQLite schema and queries (candles, alerts, snapshots, candidates, evaluations)
screener/cache.py       wraps a client: serves candles from SQLite, fetches only missing ranges
screener/live_feed.py   Dhan WebSocket client, binary packet decoder, 1-min candle builder, demo feed
screener/live_engine.py all-NSE mode: live rows for every stock + background history / deep scan
screener/push.py        WebSocket deltas: keyed state, per-browser change tracking, one shared snapshot per tick
screener/settings.py    editable-settings schema, validation, live apply, data/settings.json
screener/market_calendar.py  trading days, holidays, special sessions, market phase
screener/clock.py       the IST clock (independent of the computer's timezone)
screener/logs.py        console + JSON-lines logging, correlation ids, secret redaction
screener/token_manager.py  access-token auto-renewal: schedule, RenewToken call, .env rewrite, hot swap
screener/backtest.py    causal features, candle-close replay, trade simulation, costs, metrics, reports
static/index.html       dashboard page; static/css/app.css; static/js/app.js (state, tables, alerts, detail)
static/js/chart.js      SVG candle / MACD / RSI / ADX chart for the detail panel
static/settings.html    settings page (static/js/settings.js)
static/backtests.html   saved backtest runs (static/js/backtests.js)
tests/                  pytest suite
```
