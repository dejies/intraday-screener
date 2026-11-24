# Intraday Screener (learning project)

A local dashboard that screens a watchlist (default: NIFTY 50) on 5-minute candles,
sorts stocks into **Bullish / Bearish / Neutral** using VWAP, **MACD** and the
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

```bash
cd intraday-screener
python3 -m venv .venv                      # private Python environment for this project
.venv/bin/pip install -r requirements.txt  # pandas, numpy, flask, requests, websocket-client, pytest
.venv/bin/python -m pytest -q              # optional: runs the 51 tests (~1 minute)
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
   - Tokens expire, typically after about 24 hours. When the dashboard shows a red "credentials
     rejected" banner, generate a new token, paste it into `.env` and restart.

### 5. Choose what to scan

| Mode | Command | Stocks | Live prices from |
|---|---|---|---|
| **Watchlist** (default) | `.venv/bin/python app.py` | the 50 in `watchlist.txt` (NIFTY 50) | REST quotes every 5 s |
| Watchlist, real-time | `.venv/bin/python app.py --feed ws` | `watchlist.txt` | Dhan WebSocket |
| **All NSE stocks** | `.venv/bin/python app.py --universe nse` | every NSE equity share (3,213) | Dhan WebSocket |

- **Your own list:** edit `watchlist.txt`, one NSE symbol per line (`#` starts a comment), or keep
  several lists and pick one with `--watchlist banks.txt`.
- **Change the default mode:** set `UNIVERSE = "nse"` in `config.py`, so plain `app.py` scans all NSE stocks.

### 6. Start it

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

1. Click **Enable sound & desktop alerts** once, and allow notifications if the browser asks.
2. Click **Test alert**: a pop-up should appear, with a beep, plus a desktop notification if the tab is in the background.
3. Choose your pop-ups:
   - *RVOL spike pop-ups* turns the ⚡ volume-spike pop-ups on or off.
   - *Pop up only confirmed breakouts* hides breakout pop-ups where fewer than 4 of the 5 indicators agree. Recommended in all-NSE mode.
4. Leave the tab open. It refreshes every 3 seconds. Hover over a row for its levels, and use the
   search box to find any stock.

**Stop it** with **Ctrl+C** in the terminal. Candles and alerts are saved as they arrive, so
nothing is lost. Restarting the same day restores the day's alert log.

**Without the dashboard:** `.venv/bin/python app.py --once` runs one watchlist scan, prints the
bullish and bearish tables in the terminal, and exits.

### 7. A typical trading day

| Time (IST) | What to do |
|---|---|
| Before 08:45 | If yesterday's token has expired, generate a new one and update `.env` |
| 08:30–08:45 | *All-NSE only:* `history.py backfill --universe nse --daily-only` |
| 08:45–09:10 | Start `app.py`, enable alerts, click **Test alert** |
| 09:15–15:30 | Watch the lists and alerts. In all-NSE mode the banner shows history loading until it finishes. |
| After 15:30 | Review with `history.py alerts --days 1`, then stop with Ctrl+C. Left running, it slows its polling after the close. |

### 8. Configuration reference

**Files:**

| File | What it holds |
|---|---|
| `.env` | your Dhan client ID and access token |
| `watchlist.txt` | symbols for watchlist mode |
| `config.py` | every setting: thresholds, periods, polling, universe, port |
| `security_ids_override.csv` | optional `symbol,security_id` rows for symbols the lookup misses |
| `data/market.sqlite` | saved candles, instrument IDs and alerts (`--db` for another file, `--no-db` to disable) |
| `data/scrip_master.csv` | Dhan's instrument list, cached and re-downloaded once a day |

**Most-used `config.py` settings** (restart the app after changing any):

| Setting | Default | Change it to… |
|---|---|---|
| `UNIVERSE` | `"watchlist"` | `"nse"` to scan every NSE stock by default |
| `PORT` | 8050 | run on another port (or use `--port`) |
| `MIN_AVG_TURNOVER_CR`, `MIN_ATR_PCT` | 100, 1.5 | loosen or tighten the liquidity and volatility filters |
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

### 9. Check these on your first live run

- The header shows **50/50 symbols** (all 50 default symbols, including `TMPV`, resolve in Dhan's
  list as of 24-Nov-2026). If any are missing, they're listed under *Data errors* with the reason.
  - Only NSE equity shares are accepted. BSE-only listings, ETFs, indices, bonds and F&O contracts are skipped.
  - `BE`/`BZ` (trade-for-trade) and SME stocks are accepted, but tagged and kept out of the bullish/bearish lists.
  - Fix the spelling in `watchlist.txt`, or add a row to `security_ids_override.csv`:
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

### 10. Troubleshooting

| What you see | What to do |
|---|---|
| `DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN must be set` | `.env` is missing or empty (step 4), or run `--demo` |
| Red banner: *credentials rejected* / *access token expired* | generate a new token, update `.env`, restart |
| Error `806` / *Data APIs not subscribed* | your Data API subscription isn't active |
| `Address already in use` | the app is already running, or pick another port: `--port 8060` |
| Symbols listed under *Data errors* | fix the spelling or add an override (step 9) |
| `NotOpenSSLWarning … LibreSSL` at startup | harmless warning from macOS's built-in Python; ignore it |
| Browser didn't open | open <http://127.0.0.1:8050> yourself |
| No sound or notifications | click **Enable sound & desktop alerts**, and allow browser notifications in your system settings |
| All-NSE banner *Loading daily history* for a long time | normal on a first run (~14 min); run the pre-market backfill next time |
| *Live feed disconnected — reconnecting* | it retries on its own (up to every 30 s). Check your internet if it lasts. |
| Empty or stale tables on a weekday | probably an exchange holiday (not detected); it shows the last session |
| `Scanning every NSE stock needs the WebSocket feed` | don't combine `--universe nse` with `--feed rest` |

## History database (SQLite)

In live mode every candle the dashboard fetches is saved to `data/market.sqlite`, along with
instrument IDs and every breakout alert. The database works as a cache. Each refresh asks
Dhan only for bars that aren't stored yet, starting from the last saved bar, which is re-fetched
in case it was still forming. Restarting mid-day restores that day's alert log, so levels that
already fired stay quiet. Demo mode keeps its data in memory, so simulated prices never mix with
real history. `--no-db` turns the database off.

Build a longer history with `history.py`:

```bash
.venv/bin/python history.py backfill                       # 1 year of 5-min + 5 years of daily, watchlist + NIFTY 50
.venv/bin/python history.py backfill --days 730 --interval 5 15
.venv/bin/python history.py status                         # rows and date range per symbol
.venv/bin/python history.py export RELIANCE --interval 5 --from 2026-01-01 --out reliance_5m.csv
.venv/bin/python history.py export "NIFTY 50" --interval D
.venv/bin/python history.py alerts --days 10               # alerts the dashboard saved
```

- **Incremental and resumable.** Re-running a backfill downloads only what's missing. If it stops
  part-way (network error, expired token), run it again to continue. The `coverage` table records
  the ranges already requested, so weekends and holidays aren't requested again.
- **Cost.** Dhan serves at most 90 days of intraday data per request and about 5 years in total.
  One year of 5-min plus five years of daily candles for 51 series is about 360 requests
  (around 1.5 minutes at 4 requests a second) and about 80 MB. 1-min candles are 5× larger, so keep long backfills at 5-min.
- **Timing.** Run backfills outside market hours, or before starting the dashboard, so the two
  don't compete for Dhan's rate limit. The database itself handles both running at once.
- **Explore with SQL.** macOS includes `sqlite3`, and the `v_candles` view adds the symbol to each candle:
  ```bash
  sqlite3 -header -column data/market.sqlite \
    "SELECT ts, open, high, low, close, volume FROM v_candles
     WHERE symbol = 'RELIANCE' AND interval = 'D' ORDER BY ts DESC LIMIT 10"
  ```
  Tables: `candles` (interval `'D'` = daily, `'5'` = 5-min; timestamps in IST; intraday `ts` is the
  bar's start time), `instruments`, `coverage`, `alerts`, `meta`.

## How a stock is classified

Every formula, with worked examples, is in
[technical_indicators_calculations.md](technical_indicators_calculations.md).

Six **directional votes** are taken on 5-minute candles (the coloured dots and the EMA stack tag):

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
vote, set `EMA_STACK_VOTE = False` or `DAILY_EMA_STACK_VOTE = False`. The **score (out of 9)** is the
number of votes plus two strength checks: **ADX ≥ 20** (a real trend, not chop) and
**Day RVOL ≥ 1.5×** (more volume so far today than usual at this time of day). Each list is ranked by
score, then ADX, and the top 5 are marked **TOP**.

Stocks go to *Neutral & filtered* when their votes are mixed, or when they fail the
tradability filters: 20-day average turnover below ₹100 crore, or 14-day ATR below 1.5% of price.

## How breakouts work

A breakout fires **once per stock per level per day**, the moment the live price
crosses the level by more than 0.05%. Levels: **OR High/Low** (only after 09:30),
**PDH/PDL** (previous day's high and low) and **52W High/Low**.

- Stocks that are already past a level when you start the app don't alert. Only a fresh cross alerts.
- Each alert shows five checks: MACD, DMI, ADX, RVOL and VWAP agreeing with its direction.
  **Confirmed** means at least 4 of 5 agree. Tick *Pop up only confirmed breakouts* to cut the noise.
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
| MACD, +DI/−DI, ADX, Day RVOL, opening range → **bullish/bearish lists, breakout alerts** | the **500 most liquid stocks that pass the filters** (`DEEP_SCAN_MAX`) | 5-min candles over REST, refreshed once per 5-min bar |

Why not the full indicator scan for all 3,213? That would need about 3,200 REST requests every
5 minutes (more than 13 minutes per pass at Dhan's rate). Stocks that fail the turnover and ATR
filters (or aren't EQ series) would land in *Neutral & filtered* anyway. They still get live prices and ⚡ spike alerts.

- **Daily history.** The first run downloads 990 days of daily candles for every stock, enough
  for a settled daily EMA200 (about 3,200 requests, roughly 13–14 minutes; a database from an
  earlier version tops up the older days once). Each morning it then tops up yesterday's candle for
  each stock (also about 3,200 small requests). Your watchlist loads first, then EQ stocks, then BE/SME. Either run
  `history.py backfill --universe nse --daily-only` before 09:00, or start the app by about 08:45.
  Stocks join the lists as their history loads; the banner shows progress.
- **Bar RVOL needs 20 live candles**, so spike alerts begin about 21 minutes after the feed connects.
  Start the app before 09:15 to have them from 09:35. The candle the feed connected in is ignored
  because it's partial. After a reconnect, volume traded while disconnected is not dumped into one
  candle, so a reconnect never causes a fake spike.
- **Noise control.** A ⚡ alert needs the spike candle to trade at least ₹25 lakh
  (`RVOL_ALERT_MIN_BAR_VALUE`), which filters out thinly traded stocks. Breakout alerts only fire for
  stocks that pass the filters (`BREAKOUT_ALERTS_REQUIRE_FILTERS`). With this many stocks, ticking
  *Pop up only confirmed breakouts* is recommended.
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
- An **alert fires when Bar RVOL is greater than 2.5×**. It goes to the pop-up, the alert log,
  a desktop notification and the database. Green or red on the alert is the spike candle's own
  direction. Use the *RVOL spike pop-ups* checkbox to stop the pop-ups; they still appear in the log.
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
- **Two different RVOLs.** *Day RVOL* (today's total volume vs the same time on the previous 20
  sessions) still drives the score. *Bar RVOL* catches sudden bursts, such as block deals or news.

Settings: `RVOL_ALERT_THRESHOLD`, `RVOL_SPIKE_INTERVAL_MIN`, `RVOL_SPIKE_LOOKBACK_BARS`,
`RVOL_ALERT_FROM`, `RVOL_ALERT_COOLDOWN_MIN`, `RVOL_ALERT_MAX_AGE_MIN` and `RVOL_SPIKE_ENABLED`
in `config.py`.

## Tuning

Every threshold lives in `config.py`, including the candle interval, opening-range length,
MACD/DMI periods, filters, the vote and score rules, breakout buffer and polling rates.
Treat each value as a hypothesis to test, not a rule.

## Known limitations

- The WebSocket client is tested against a fake Dhan server (connect, subscribe batches,
  binary packets, reconnect, token-expiry disconnect) and its packet decoding matches the
  official `dhanhq` SDK, but it has not yet been run against the real Dhan feed.

- Exchange holidays aren't detected. On a holiday the screen shows "waiting" with no candles.
- Breakouts trigger on price ticks, not candle closes, so some will reverse straight away
  (false breakouts). A good exercise is to add a candle-close confirmation option.
- If Dhan includes the still-forming candle, the last bar's indicators change until it closes.
- Day RVOL averages the previous 20 sessions (`RVOL_LOOKBACK_SESSIONS`). To have them, the screener
  loads 35 calendar days of 5-min candles per stock (`WARMUP_CALENDAR_DAYS`). That's one request per
  stock, and an existing database tops up the extra days once. A stock with less history, such as a
  recent listing, uses the sessions it has.
- Demo-mode prices are random walks. They exercise the code but teach nothing about real markets.

## Project layout

```
app.py                  Flask server, CLI, background polling thread
history.py              backfill / status / export / alerts for the SQLite history
config.py               all thresholds and settings (+ .env loader)
watchlist.txt           symbols to screen
screener/indicators.py  EMA, MACD, ATR, +DI/-DI/ADX, VWAP, opening range, RVOL
screener/engine.py      metrics, classification, breakout tracker, polling loop
screener/dhan_client.py read-only DhanHQ v2 client (instruments, candles, LTP)
screener/demo_client.py simulated market with the same interface
screener/store.py       SQLite schema and queries (candles, coverage, instruments, alerts)
screener/cache.py       wraps a client: serves candles from SQLite, fetches only missing ranges
screener/live_feed.py   Dhan WebSocket client, binary packet decoder, 1-min candle builder, demo feed
screener/live_engine.py all-NSE mode: live rows for every stock + background history / deep scan
static/index.html       dashboard (vanilla JS, polls /api/state every 3 s)
tests/                  pytest suite
```
