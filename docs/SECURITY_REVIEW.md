# Security review (25-Nov-2026, step 8)

Scope: the whole project as of step 8: a single-user app that runs on your own computer, reads market
data from Dhan, and shows it in your browser. It never places orders. This is a self-review of the code
and configuration, with the tests that back each control. It isn't an external audit.

## What needs protecting, and from whom

| Asset | Threat |
|---|---|
| Dhan client id and access token | leaking into logs, backups, the browser, git, or other users on the computer |
| Your trading account | any code path that could place, modify or cancel orders |
| The dashboard and its local API | other websites open in the same browser: cross-site requests, DNS rebinding, cross-site WebSocket, clickjacking, script injection |
| Saved data (candles, alerts, settings, backtests) | corruption, loss, path tricks |

## Controls and evidence

| # | Control | Where | Evidence |
|---|---|---|---|
| 1 | **No order placement.** The client only calls `/charts/historical`, `/charts/intraday`, `/data/companyinfo`, `/marketfeed/ltp` and the market-data WebSocket. No order, position, holding or funds endpoint exists in the code. | `screener/dhan_client.py`, `live_feed.py` | code search (`/v2/orders` etc.: no matches) |
| 2 | Credentials only in `.env` (git-ignored); loaded into `config`; never in settings, snapshots, `/health` or pages. | `config.py`, `screener/settings.py` | `test_dashboard.py::test_schema_has_no_secrets_or_paths`, `…test_pages_health_and_local_host_only` (token value absent from every payload) |
| 3 | Logs redact the token, client id, `token=` parameters, `access-token` / `Authorization` headers and JWT-shaped strings, in messages and tracebacks; the feed also redacts its URL. | `screener/logs.py`, `live_feed.py` | `test_hardening.py::test_redactor_masks_secrets_everywhere`, `…test_json_log_file_has_ids_and_no_secrets` |
| 4 | `manage.py check` reports whether credentials are set and the token's expiry without printing them; warns when `.env` is readable by others. `scripts/setup.sh` creates `.env` with mode 600. | `manage.py` | `test_hardening.py::test_check_never_prints_the_token` |
| 5 | Backups leave out `.env` unless `--include-env`; then it's copied with mode 600. | `manage.py` | `test_hardening.py::test_backup_is_consistent_and_leaves_out_env` |
| 6 | Server listens on `127.0.0.1` only. | `config.HOST` | — |
| 7 | **DNS rebinding:** requests must carry a local `Host` (TrustedHostMiddleware). | `app.py` | `test_dashboard.py::test_pages_health_and_local_host_only` (400 for `evil.example`) |
| 8 | **Cross-site requests:** every state-changing call (`POST /api/alerts/ack`, `POST /api/settings`) requires `Content-Type: application/json`, which a foreign page can't send without a CORS preflight; the server sends no CORS headers. | `app.py` | `test_alerts.py::test_alert_api`, `test_dashboard.py::test_settings_api_applies_live_and_saves` (415 for a form post) |
| 9 | **Cross-site WebSocket:** `/ws` refuses connections whose `Origin` isn't this server. Browser → server messages are ignored; 1 MB frame limit. | `app.py` | `test_dashboard.py::test_websocket_full_then_delta` |
| 10 | **Script injection:** server values go through `esc()` before `innerHTML`; a Content-Security-Policy allows scripts only from this server (no inline scripts or handlers, no CDNs, no `eval`), so an injected `<script>` or `onerror=` wouldn't run. | `static/js/*.js`, `app.py` | `test_hardening.py::test_request_ids_and_security_headers`, `…test_pages_have_no_inline_scripts` |
| 11 | **Clickjacking:** `frame-ancestors 'none'` and `X-Frame-Options: DENY`; also `nosniff`, `Referrer-Policy: no-referrer`, `Cross-Origin-Opener-Policy`. | `app.py` | as above |
| 12 | **Path traversal:** backtest reports are looked up by a generated id matching `^\d{8}-\d{6}-[0-9a-f]{6}$`; static files through Starlette's `StaticFiles`. | `app.py` | `test_backtest.py::test_backtest_api` |
| 13 | **SQL injection:** every query uses `?` parameters; the only formatted SQL builds placeholder lists or uses table names from constants. | `screener/store.py` | code review |
| 14 | **Settings input:** only whitelisted keys, type- and range-checked, validated together, applied all-or-nothing. | `screener/settings.py` | `test_dashboard.py::test_coerce_validation`, `…test_apply_is_all_or_nothing_and_copy_on_write` |
| 15 | Dhan rate limits respected (4 data requests / s, 1 quote request / s); no bulk API calls without a command you run. | `dhan_client.py` | — |

## Findings and recommendations

| Severity | Finding | Recommendation |
|---|---|---|
| Medium | `.env` on this computer is mode 0644 (readable by other users of the Mac). | `chmod 600 .env` (`manage.py check` warns until it's done). |
| Medium | The virtual environment uses macOS' system **Python 3.9**, which reached end of life in October 2025 (no more security fixes) and is built with LibreSSL 2.8.3. | Recreate `.venv` with Python 3.12 or 3.13 from python.org or Homebrew: `rm -rf .venv && PYTHON=python3.12 scripts/setup.sh`. The code runs on 3.9+. |
| Low | The dashboard has no login. That's acceptable while it listens only on `127.0.0.1`. | Don't change `HOST` to `0.0.0.0` or expose the port to a network. To use it from another device, use an SSH tunnel. |
| Low | The access token sits in plain text in `.env` (inherent to Dhan's API-token model); Dhan tokens expire after about a day, which limits exposure. | Keep `.env` out of backups you share; regenerate the token if you think it leaked. |
| Low | Dependencies use version ranges, not hashes. | Run `pip install pip-audit && pip-audit -r requirements.txt` now and then; update within the ranges. |
| Info | `style-src` allows inline styles (the pages use `style=` attributes). Styles can't run code, so this doesn't weaken the script protections. | — |
| Info | The live WebSocket client has only been tested against a fake server, not the real Dhan feed. | Do the manual first-live-day checks in [OPERATIONS.md](OPERATIONS.md#7-recovery-playbook). |

## Configuration backup

`manage.py backup` (or `scripts/backup.sh`) copies the database and every configuration file except `.env`;
see [OPERATIONS.md §6](OPERATIONS.md#6-backups-and-restore).
