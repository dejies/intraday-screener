# Operations: running, monitoring, backups, recovery and troubleshooting

How to run the screener day to day, read its logs, back it up, and recover when something breaks.
Setup and configuration are in the [README](../README.md); every formula is in
[technical_indicators_calculations.md](../technical_indicators_calculations.md).

## 1. Install and start

```bash
scripts/setup.sh          # once: .venv, dependencies, a .env template (chmod 600), then `manage.py check`
scripts/demo.sh           # simulated market, no account
scripts/start.sh          # your Dhan data (runs `manage.py check --quick` first); extra args go to app.py
scripts/start.sh --universe nse --no-browser
```

The same, by hand: `.venv/bin/pip install -r requirements.txt` then `.venv/bin/python app.py …`.
On Windows the same scripts exist as `scripts\setup.bat`, `demo.bat`, `start.bat`, `backup.bat`, plus
`check.bat` (= `manage.py check`) and `run.bat` (any command, e.g. `scripts\run.bat history.py status`);
see the README's *On Windows* section.
For development, `pip install -e ".[test]"` also installs the commands `screener`, `screener-history`,
`screener-backtest`, `screener-universe` and `screener-manage` (editable install only: `config.py` finds
`data/`, `static/` and the CSV files next to itself).

## 2. A trading day

| When (IST) | What | Command |
|---|---|---|
| Before 08:45 | Only after a day or more with the app stopped: new access token if it expired (the app renews it otherwise; `manage.py check` shows the expiry) | edit `.env` |
| 08:30 (all-NSE mode) | Daily candles for every stock | `history.py backfill --universe nse --daily-only` |
| 08:45–09:10 | Check, then start; plug the Mac in and keep the lid open | `scripts/start.sh` |
| 09:15–15:30 | Watch; the 🔔 badge and CRITICAL toasts flag problems | dashboard |
| After 15:30 | Review alerts; back up; stop with Ctrl+C | `history.py alerts --days 1`, `scripts/backup.sh` |
| Weekly | New listings and sectors | `universe.py build` |
| December | Add next year's NSE holidays | edit `nse_holidays.csv` (§5) |

On an NSE holiday the header shows **NSE holiday: …**, the banner says the market is closed, polling
slows down, and the tables show the last session.

## 3. Checks and diagnostics

```bash
.venv/bin/python manage.py check          # everything below; exit code 1 when something is broken
.venv/bin/python manage.py check --quick  # skips the database integrity scan and the running-app probe
curl -s http://127.0.0.1:8050/health      # ok / degraded (HTTP 503) for scripts
```

`check` covers: Python and package versions; `.env` present and private (`chmod 600 .env`); whether the
client id and token are set and when the token expires (decoded locally, never printed); the watchlist,
sector mapping and saved settings; holiday-calendar coverage (warns when this year or the next 30 days
aren't covered); database schema version, `PRAGMA quick_check`, candle coverage per segment and interval,
index 5-min candles for backtests, WAL size; free disk space; whether the dashboard is running and healthy;
errors in the recent log.

## 4. Logs

| Where | What |
|---|---|
| terminal | readable lines, IST time |
| `logs/screener.jsonl` | one JSON object per line, rotated at 5 MB (5 files kept) |

Each JSON record has `ts_utc`, `ts_ist`, `level`, `logger`, `msg`, `run_id` (one per start of the app),
`request_id` (the dashboard request; the response carries it as `X-Request-ID`), `cycle` (the engine refresh)
and `exc` (traceback). The access token, client id, `token=` URL parameters, `access-token` headers and
anything shaped like a JWT are replaced by `***` before anything is written, tracebacks included.

```bash
grep '"level": "ERROR"' logs/screener.jsonl | tail
.venv/bin/python -c "import json; [print(r['ts_ist'], r['level'], r['msg']) for r in map(json.loads, open('logs/screener.jsonl')) if r['level'] != 'INFO']"
grep '"request_id": "3f2a9c1d0b7e"' logs/screener.jsonl     # everything one request did
```

`python app.py -v` logs at DEBUG; `LOG_LEVEL` in `config.py` sets the default.

## 5. Holiday calendar

`nse_holidays.csv` (versioned with the code):

```
date,kind,description,open,close,source
2026-12-25,holiday,Christmas,,,broker lists (Upstox; ICICI Direct) cross-checked 24-Nov-2026
2026-11-08,special,Muhurat trading,18:00,19:00,NSE circular …
```

- `holiday`: a weekday with no trading. Weekend holidays need no row.
- `special`: a session on a normally closed day (Muhurat trading, a Budget-day Sunday). It trades between
  `open` and `close`; without times it counts as closed (NSE announces Muhurat timings in a separate circular).
- NSE publishes next year's list in December (on nseindia.com, *Resources → Exchange communication →
  Holidays*). Add the rows; `manage.py check` and the dashboard banner warn when the year isn't covered,
  and until then weekdays count as trading days.
- The 2026 rows were cross-checked between two broker pages on 24-Nov-2026 because NSE's PDF didn't load
  automatically. Check them against the official circular when you can.

## 6. Backups and restore

```bash
scripts/backup.sh                   # = manage.py backup -> backups/YYYYMMDD-HHMMSS/
.venv/bin/python manage.py backup --keep 10
.venv/bin/python manage.py backup --include-env      # adds .env (your token): keep that backup private
```

A backup holds `market.sqlite` (copied with SQLite's online-backup API, so it's consistent even while the app
writes), `data/settings*.json`, `watchlist.csv`, `nifty50.txt`, `sector_indices.csv`, `nse_holidays.csv`,
`security_ids_override.csv`, `data/backtests/` and a `MANIFEST.json`. `.env` is left out unless asked.

Each backup is a full copy of the database. After a full backfill (every stock, a year of 5-min candles)
that's about 4 GB, so keep only a few: `scripts/backup.sh --keep 3`.

**Restore:** stop `app.py`, copy `market.sqlite` (and `settings*.json`) back into `data/`, then start. The
schema upgrades itself if the backup is from an older version.

## 7. Recovery playbook

| Symptom | What happens automatically | What to do |
|---|---|---|
| Dhan rejects the token (HTTP 401/403, or the feed's invalid-token disconnect) | CRITICAL **Credentials** alert; red banner; polling slows | new token in `.env`: the running app loads it within a minute and restarts the feed |
| Token about to expire | renewed at the scheduled times, at startup / exit, and as a safety net in its last hour (README, *Access-token auto-renewal*) | nothing |
| Token renewal fails | CRITICAL **Access token** alert; retried every 5 min until the token expires | check the network; if it keeps failing, a new token in `.env` |
| Renewed but `.env` couldn't be written | CRITICAL alert; the new token works until the app stops | fix the file permissions, then **Renew now** on the Settings page |
| Live feed disconnects | reconnects with backoff (1 s → 30 s); CRITICAL alert after 10 s, LOW when back | nothing, unless it keeps failing: check the network and `manage.py check` |
| Feed connected but silent while open | DATA STALE + CRITICAL after 10 s; **forced reconnect** after 60 s (`FEED_RECONNECT_STALE_SECONDS`), at most once a minute | if it repeats, restart; check Dhan's status |
| REST price / candle calls fail | the loop keeps running; one MEDIUM *Data error* alert counts the repeats; stale prices go CRITICAL | check network / rate limits; errors panel lists the details |
| The Mac slept (idle, or the lid closed) | everything pauses; on wake: HIGH **Computer asleep** alert with the times, banner, immediate feed reconnect, the full scan catches up (most active stocks first) | keep the lid open and the Mac on power during market hours; `PREVENT_SLEEP` (on by default) stops idle sleep |
| 5-min refresh stuck (network, DNS, Dhan) | each request gives up after 30 s; CRITICAL **Indicator refresh** alert after 2 min without progress (`ALERT_RULES.refresh.after_seconds`), INDICATORS BEHIND badge, LOW alert when it moves again | check the network; `manage.py check`; the errors panel |
| App crashed or stopped mid-day | on restart: candles from SQLite, today's alerts, acknowledgements and candidates restored; nothing re-fires (unique keys) | `scripts/start.sh` |
| `data/settings.json` broken or invalid | ignored with a warning; `config.py` defaults used | fix it on the settings page, or delete it |
| Database deleted | rebuilt empty; candles re-fetched as needed | restore a backup to keep alert / backtest history |
| Database damaged | `manage.py check` reports `quick_check` problems | stop the app, restore the latest backup |
| Holiday not in the calendar | the screener expects a session: no new candles (and DATA STALE in live-feed mode, as no ticks arrive) | add the row to `nse_holidays.csv`, restart |
| Port 8050 busy | `app.py` exits with an address-in-use error | `--port 8060`, or stop the other program |

### Recovery test plan

| Scenario | Automated test |
|---|---|
| Restart mid-day restores alerts, acknowledgements, candidates; no duplicate events | `test_screener.py::test_alerts_saved_and_restored_after_restart`, `test_strategies.py::test_restart_restores_candidates_without_duplicate_events`, `test_alerts.py::test_persist_restore_ack_and_bump` |
| Engine loop survives failing data calls and recovers | `test_hardening.py::test_engine_loop_survives_data_failures_and_recovers` |
| Feed disconnect / reconnect, token-expiry disconnect | `test_live.py::test_dhan_feed_over_a_real_websocket_connection` (fake server) |
| Token renewal: schedule, retries, `.env` rewrite, hot swap, feed restart, `.env` edited by hand | `test_token.py` |
| Silent feed is reconnected, once per interval | `test_hardening.py::test_feed_watchdog_forces_one_reconnect_per_interval`, `…force_reconnect_closes_the_socket` |
| Feed / stale / recovery alerts | `test_alerts.py::test_feed_monitor_waits_then_recovers`, `…test_engine_flags_stale_rest_prices` |
| Old databases upgrade in place | `test_alerts.py::test_schema_v6_upgrade_keeps_old_alerts`, `test_strategies.py::test_schema_upgrade_and_prune` |
| Missing database is rebuilt | `test_hardening.py::test_missing_database_is_recreated` |
| Bad saved settings are skipped | `test_dashboard.py::test_save_and_load_round_trip` |
| Full scan fetched right after each candle closes, most active first; stall / sleep alerts; request deadline | `test_refresh.py` |
| Holidays and special sessions | `test_hardening.py::test_calendar_phases_and_trading_days`, `…test_snapshot_on_a_holiday` |
| Backups are consistent and leave out `.env` | `test_hardening.py::test_backup_is_consistent_and_leaves_out_env` |

**Manual checks on the first live day** (they need the real Dhan feed, which tests can't reach): start
before 09:15 and confirm the feed badge turns *live*; pull the network cable or turn off Wi-Fi for 30 s and
confirm the CRITICAL alert, the reconnect and the LOW *recovered* alert; restart `app.py` at 11:00 and
confirm the alert centre and candidates come back without new toasts. After the close, click **Renew now**
on the Settings page once: confirm the LOW *Token renewed* alert, the new expiry, that `.env` changed, and
that the feed badge goes back to *live* within a few seconds (this is the first real `RenewToken` call).

## 8. Troubleshooting

| Problem | Likely cause | Fix |
|---|---|---|
| "Dhan rejected the credentials" / "token expired" | the app was stopped for over a day, or renewal failed | new token at web.dhan.co, put it in `.env` (picked up within a minute) |
| World news tab: a source shows ✗ | that site refused or changed its feed (the others still work) | wait for the next update; if it stays ✗, switch it off on the Settings page (*World news*) |
| World news tab is empty | first start without internet, or `NEWS_ENABLED` off | check the internet connection, then **Refresh now** |
| A `history.py` / `backtest.py` run fails with an auth error | the app renewed the token while it ran | run it again (it reads `.env` at start) |
| Nothing updates, "NSE holiday" in the header | exchange holiday | nothing; the tables show the last session |
| DATA STALE during market hours | network, Dhan outage, or (live feed) a holiday missing from the calendar | check the network; `manage.py check`; add the holiday |
| Strong Momentum / Sector Leaders never qualify | sector RS unavailable: composite sector with < 3 liquid stocks, or no index data | normal for small watchlists; in backtests run `history.py backfill --watchlist nifty50.txt` (fetches index candles too) |
| Day RVOL shows "—" | fewer than 10 earlier sessions of 5-min candles | wait, or `history.py backfill --watchlist nifty50.txt --days 60` |
| Toasts but no sound | browsers block audio until you click | click **Sound** once after each page load |
| No desktop notifications | permission not granted, or the tab is in front | allow notifications; they're sent while the tab is in the background |
| Page shows "polling" instead of "live push" | the WebSocket was refused or dropped | it falls back to polling automatically; check that you opened `127.0.0.1:8050`, not another host name |
| `manage.py check` warns about `.env` permissions | the file is readable by other users | `chmod 600 .env` |
| `address already in use` | another app on port 8050 | `--port 8060` |
| Settings change has no effect | it needs a restart (marked *restart*) | restart `app.py` |
| A backtest has no testable sessions | fewer than 11 sessions of 5-min candles | `history.py backfill --watchlist nifty50.txt --days 120` |

Still stuck: run `manage.py check`, look at `logs/screener.jsonl` around the time it happened (filter by
`run_id` or `request_id`), and check the *Data errors* panel at the bottom of the dashboard.
