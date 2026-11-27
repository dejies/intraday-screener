# Technical indicators: how every value is calculated

This document explains every indicator and derived value the screener uses, how each one is
calculated, which candles it uses, and how it feeds the bullish/bearish lists and the alerts.

- **Code:** `screener/indicators.py` (formulas) and `screener/engine.py` (how they're combined).
- **Settings:** every period and threshold below is a setting in `config.py`. Defaults are shown.
- **Worked examples:** each one was run through the project's own functions to check the numbers.

> For learning only. These are mechanical rules, not predictions or investment advice.

---

## Contents

1. [Conventions](#1-conventions)
2. [Quick reference](#2-quick-reference)
3. [Building blocks: EMA and Wilder's smoothing](#3-building-blocks-ema-and-wilders-smoothing)
4. [MACD (12, 26, 9)](#4-macd-12-26-9)
5. [EMA 9, 20, 50 and 200](#5-ema-9-20-50-and-200)
6. [True Range and ATR](#6-true-range-and-atr)
7. [Directional Movement Index: +DI, −DI, ADX](#7-directional-movement-index-di-di-adx)
8. [RSI (14)](#8-rsi-14)
9. [VWAP](#9-vwap)
10. [Opening range (first 15 minutes)](#10-opening-range-first-15-minutes)
11. [Daily levels: PDH, PDL, PDC, 52-week high/low, pivots and CPR](#11-daily-levels-pdh-pdl-pdc-52-week-highlow-pivots-and-cpr)
12. [Change %, gap % and relative strength](#12-change--gap--and-relative-strength)
13. [Average turnover](#13-average-turnover)
14. [Day RVOL and 5m RVOL (time-of-day relative volume)](#14-day-rvol-and-5m-rvol-time-of-day-relative-volume)
15. [Bar RVOL (volume-spike relative volume)](#15-bar-rvol-volume-spike-relative-volume)
16. [How 1-minute candles are built from the live feed](#16-how-1-minute-candles-are-built-from-the-live-feed)
17. [Optional confirmation metrics: Bollinger, OBV, MFI](#17-optional-confirmation-metrics-bollinger-obv-mfi)
18. [Putting it together: votes, score, bias, filters, ranking](#18-putting-it-together-votes-score-bias-filters-ranking)
19. [Strategies, setup score and candidate lifecycle](#19-strategies-setup-score-and-candidate-lifecycle)
20. [Breakout alerts](#20-breakout-alerts)
21. [Volume-spike (Bar RVOL) alerts](#21-volume-spike-bar-rvol-alerts)
22. [Alert centre: rules, severity, deduplication](#22-alert-centre-rules-severity-deduplication)
23. [Backtesting: replay, trades, costs, metrics](#23-backtesting-replay-trades-costs-metrics)
24. [Data windows and warm-up](#24-data-windows-and-warm-up)
25. [Why values can differ from TradingView or your broker's chart](#25-why-values-can-differ-from-tradingview-or-your-brokers-chart)
26. [All settings](#26-all-settings)

---

## 1. Conventions

| Item | Convention |
|---|---|
| Time zone | All timestamps are IST (Asia/Kolkata). |
| Candle timestamp | The candle's **start** time. The `09:15` 5-min candle covers 09:15:00–09:19:59. |
| Session | NSE cash market, 09:15–15:30 IST, from the computer's clock converted to IST (any timezone setting works). Holidays and special sessions (e.g. Muhurat trading) come from `nse_holidays.csv`; on a holiday the session shown is the previous trading day. |
| "Session day" | From 09:00 on a weekday it's **today**, even before the first candle exists. Otherwise it's the latest trading day in the data. So at 09:05, PDH means *yesterday's* high. |
| Indicator candles | **5-minute** candles for MACD, RSI, EMA 9/20/50/200, DMI/ADX, intraday ATR, VWAP (REST mode), opening range, Day RVOL, 5m RVOL, Bollinger, OBV and MFI. **15-minute** candles (built from the 5-min ones) for the MACD context. **1-minute** candles for Bar RVOL. **Daily** candles for ATR, turnover, price levels, pivots/CPR and the daily EMA set. |
| "Completed" candle | A candle is complete once `now ≥ start + interval`. **Every momentum and volume indicator uses completed candles only**: MACD, RSI, DMI/ADX, EMAs, ATR, Bollinger, OBV, MFI, Day RVOL, 5m RVOL and Bar RVOL. The still-forming candle is ignored, so values don't flicker during the candle. Price-based values (LTP, VWAP distance, opening range, day high/low) use the latest price. |
| "Unavailable" | An indicator without enough candles is shown as **—** and given no vote, never as 0. Each section says how many candles it needs. |

**Where the data comes from:**

| Value | Watchlist mode (REST) | All-NSE mode (`--universe nse`, WebSocket) |
|---|---|---|
| Last traded price (LTP) | REST LTP quote every 5 s | WebSocket Quote packet (real time) |
| VWAP | Calculated from 5-min candles (§9) | Dhan's **ATP** (average trade price) from the feed |
| 1-min candles (Bar RVOL) | REST 1-min candles | Built from the live ticks (§16) |
| 5-min candles (MACD, DMI, OR, Day RVOL) | REST, every 60 s | REST, once per 5-min bar, for the 800 most active EQ stocks only (expected turnover today) |
| Daily candles | REST, once per day | REST, once per day, for every stock |
| Previous close | Daily candles | Daily candles, or the feed's Prev Close packet until those load |

---

## 2. Quick reference

| Indicator | Candles | Default settings | Used for | Code |
|---|---|---|---|---|
| EMA | — | span *n*, α = 2/(n+1) | inside MACD | `ema()` |
| Wilder's smoothing (RMA) | — | α = 1/period, seeded with a simple average | inside RSI, ATR, DMI, ADX | `rma()` |
| MACD line, signal, histogram | 5-min | 12, 26, 9 | MACD vote, breakout confirmation, cross tag | `macd()` |
| MACD state | 5-min | — | arrow in the MACD column (▲ ↑ ↗ ↓) | `macd_state()` |
| MACD context | 15-min | 12, 26, 9 | MACD tooltip | `resample_candles()`, `macd_state()` |
| MACD cross | 5-min | last 3 bars | "× ↑ / × ↓" tag | `recent_macd_cross()` |
| RSI | 5-min | 14, slope over 3 candles | RSI column (context, no vote) | `rsi()` |
| EMA 9 / 20 / 50 / 200 | 5-min | spans 9, 20, 50, 200 | EMA vote (stack), EMA 5m column: dots, stack, 9×20 cross | `ema_values()`, `ema_stack()`, `recent_cross()` |
| EMA alignment, EMA20 slope | 5-min | slope over 3 candles | EMA 5m tooltip | `_ema_alignment()`, `pct_change_over()` |
| Daily EMA 9 / 20 / 50 / 200 | daily + live price | spans 9, 20, 50, 200 | EMA day column: dots, stack, 50×200 cross (optional vote) | `compute_daily_stats()`, `live_daily_emas()` |
| True Range | daily, 5-min | — | inside ATR and DMI | `true_range()` |
| ATR % | daily | 14 | filter (≥ 1.5%) | `atr()` → `compute_daily_stats()` |
| Intraday ATR % | 5-min | 14 | row tooltip | `atr()` → `compute_intraday()` |
| +DI / −DI | 5-min | 14 | DMI vote, confirmation | `dmi()` |
| ADX, ADX rising | 5-min | 14, slope over 3 candles | strength point (≥ 20), confirmation, ↗/↘ | `dmi()`, `change_over()` |
| VWAP | 5-min / live | session-anchored | VWAP vote, confirmation | `session_vwap()` or feed ATP |
| VWAP distance, slope | 5-min / live | slope over 15 min | VWAP % column, ↗/↘ | `compute_intraday()`, `_vwap_fields()` |
| VWAP reclaim / loss | live price | 0.1% buffer, 60 s hold | "reclaim" / "lost" tag | `_vwap_fields()` |
| Opening range high/low | 5-min | first 15 min | ORB vote, breakout levels | `opening_range()` |
| PDH / PDL / PDC | daily | previous session | breakout levels, change % | `compute_daily_stats()` |
| 52-week high/low | daily | 365 days | breakout levels | `compute_daily_stats()` |
| Pivots, CPR | daily | previous session | row tooltip | `cpr_pivots()` |
| Change %, Gap % | live + daily | — | display, relative strength | `_base_rows()` |
| Relative strength (RS) | live | vs NIFTY 50, ±0.2 | RS vote | `_finish_rows()` |
| Returns 5/15/30/60 min, day | 1-min / 5-min / live | common 5-min-grid start | sector strength | `horizon_returns()` |
| Sector RS vs NIFTY 50 | index or composite | 10 indices + 12 composites | Sector RS column, sector panel | `sector_strength()` |
| Stock vs sector, percentile | live | — | vs Sector column (P = percentile) | `sector_strength()` |
| Market breadth | live | — | header breadth badge | `market_breadth()` |
| Strategies (MOM, BRK, LDR) | all of the above | `STRATEGIES` | strategy tabs, tags, candidate lifecycle | `strategies.evaluate()` |
| Setup score | all of the above | 100 points, 8 components | candidate ranking, Setup score column | `scoring.setup_score()` |
| Average turnover | daily | 20 days | optional filter (`MIN_AVG_TURNOVER_CR`, off by default) | `compute_daily_stats()` |
| Day RVOL (cumulative) | 5-min | previous 20 sessions, min 10 | strength point (≥ 1.5×), confirmation | `cumulative_rvol()` |
| 5m RVOL (same slot) | 5-min | previous 20 sessions, min 10 | RVOL day · 5m column | `slot_rvol()` |
| Bar RVOL | 1-min | 20 candles | ⚡ alert (> 2.5×), Bar RVOL column | `bar_rvol_series()`, `bar_rvol_recent()` |
| Bollinger bandwidth, %B, squeeze | 5-min | 20, 2σ; squeeze = lowest 10% of 120 | row tooltip | `bollinger()` |
| OBV trend | 5-min | change over 10 candles | row tooltip | `obv()` |
| MFI | 5-min | 14 | row tooltip | `mfi()` |

---

## 3. Building blocks: EMA and Wilder's smoothing

### 3.1 Exponential moving average (EMA)

```
α = 2 / (span + 1)
EMA[0] = price[0]
EMA[t] = EMA[t−1] + α × (price[t] − EMA[t−1])
```

The code uses `pandas.ewm(span=n, adjust=False)`, which is the recursion above, seeded with the first value.

| span | α |
|---|---|
| 9 | 0.2000 |
| 12 | 0.1538 |
| 26 | 0.0741 |

**Example** (span 3, so α = 0.5), closes 100, 102, 101, 105:

| t | close | EMA |
|---|---|---|
| 0 | 100 | 100 (seed) |
| 1 | 102 | 100 + 0.5 × (102 − 100) = **101** |
| 2 | 101 | 101 + 0.5 × (101 − 101) = **101** |
| 3 | 105 | 101 + 0.5 × (105 − 101) = **103** |

### 3.2 Wilder's smoothing (RMA)

This is the same recursion with α = 1/period. It reacts more slowly than an EMA of the same length.

```
RMA[t] = RMA[t−1] + (value[t] − RMA[t−1]) / period
```

**Seed.** As in Wilder's book, TradingView and StockCharts, the first value is the **simple average of the first
`period` values**. Earlier positions are unavailable. After the seed, the code uses
`pandas.ewm(alpha=1/period, adjust=False)`.

```
RMA[period−1] = mean(value[0 … period−1])
RMA[t]        = RMA[t−1] + (value[t] − RMA[t−1]) / period      for t ≥ period
```

**Example** (period 3), values 2, 4, 6, 8 → RMA = —, —, **4.0** (mean of 2, 4, 6), then 4.0 + (8 − 4.0) / 3 = **5.33**.
With period 14: previous RMA 2.0, new value 3.4 → 2.0 + (3.4 − 2.0) / 14 = **2.1**.

---

## 4. MACD (12, 26, 9)

Calculated on 5-min closes, including the warm-up history from earlier sessions (§24).

```
MACD line  = EMA12(close) − EMA26(close)
Signal     = EMA9(MACD line)
Histogram  = MACD line − Signal
```

**Example** (illustrative values for the latest 5-min candle):

| Step | Value |
|---|---|
| EMA12 | 1,512.40 |
| EMA26 | 1,508.10 |
| MACD line | 1,512.40 − 1,508.10 = **4.30** |
| Signal (previous 2.80, α = 0.2) | 2.80 + 0.2 × (4.30 − 2.80) = **3.10** |
| Histogram | 4.30 − 3.10 = **+1.20**, so momentum is bullish |

Only **completed** candles are used, and MACD is published once there are at least 26 + 9 = **35** of them
(the warm-up history from earlier sessions provides these at the open).

### 4.1 MACD state

| State | Rule | Arrow |
|---|---|---|
| **strong bullish** | line > 0, histogram > 0 and higher than on the previous candle | ▲ (green) |
| **bullish, weakening** | line > 0, histogram > 0 but lower than on the previous candle | ↑ (grey) |
| **early bullish reversal** | line ≤ 0 while the histogram is > 0, or a bullish cross happened in the last 3 candles | ↗ (green) |
| **bearish** | histogram < 0 | ↓ (red) |
| **neutral** | anything else (histogram exactly 0) | → |

**Example:** line +2.10, histogram +0.40, previous histogram +0.55 → the line is above zero and the
histogram is positive but shrinking, so the state is **bullish, weakening**. The momentum is still up but
fading, which often comes before a cross down.

### 4.2 15-minute context

The completed 5-min candles are grouped into 15-min candles (09:15, 09:30, 09:45 …; a group needs its
last 5-min candle to be complete) and the same MACD and state are calculated on them. This takes 35
fifteen-minute candles, about five sessions of warm-up. The 15-min state appears in the MACD tooltip
only; it isn't a vote.

**How it's used:**

- **MACD vote:** bullish if `MACD line > Signal` (histogram > 0), bearish if below.
- **Breakout confirmation:** histogram > 0 for an upside breakout, < 0 for a downside one.
- **Cross tag (× ↑ / × ↓):** the histogram changed sign within the last **3** candles and is still on
  the new side. Histograms `−0.4, −0.1, +0.2, +0.5` give **bullish**. Histograms
  `+0.3, −0.2, +0.1, −0.1` give **bearish**: the latest sign change wins, and it must match the current sign.

The histogram is in rupees, so compare it within one stock, not across stocks.

---

## 5. EMA 9, 20, 50 and 200

These are calculated on the same 5-min closes and warm-up history as MACD, using the EMA formula from §3.1.

```
EMA9, EMA20, EMA50, EMA200 = EMA(close, span) for span = 9, 20, 50, 200
an EMA is shown only once there are at least `span` candles (otherwise "—")
```

| span | α = 2/(span+1) | covers roughly |
|---|---|---|
| 9 | 0.2000 | the last 45 minutes |
| 20 | 0.0952 | the last 1 hour 40 minutes |
| 50 | 0.0392 | the last 4 hours (about two-thirds of a session) |
| 200 | 0.00995 | the last 1,000 minutes (about 2.7 sessions) |

On 5-min candles, EMA200 is a **2–3 day trend line**, not the daily 200-EMA that long-term investors watch.

**What the *EMA 5m* column shows:**

| Element | Rule |
|---|---|
| Four dots (9, 20, 50, 200) | green if the live price is above that EMA, red if below |
| **stack ↑** | price > EMA9 > EMA20 > EMA50 > EMA200, a clean uptrend on every horizon |
| **stack ↓** | price < EMA9 < EMA20 < EMA50 < EMA200, a clean downtrend |
| no stack tag | mixed: the EMAs are not in order |
| **9×20 ↑ / ↓** | EMA9 − EMA20 changed sign within the last 3 candles and is still on the new side (the same rule as the MACD cross in §4) |

**Example 1 (stacked):** price 890.80, EMA9 887.12, EMA20 881.57, EMA50 873.27, EMA200 863.02.
Since 890.80 > 887.12 > 881.57 > 873.27 > 863.02, all four dots are green and the column shows **stack ↑**.

**Example 2 (mixed):** price 1,500, EMA9 1,495, EMA20 1,488, EMA50 1,490, EMA200 1,470.
Price is above all four EMAs, so all four dots are green. But EMA20 (1,488) is below EMA50 (1,490), so
there's **no stack** tag: the short-term average has dipped under the medium one.

**How it's used:**

- **EMA vote** (§18.1, `EMA_STACK_VOTE`): stack ↑ is a bullish vote, stack ↓ a bearish vote, and mixed gives no vote.
- **Context only:** the individual dots and the 9×20 tag aren't part of the score, and the EMAs aren't a breakout confirmation. In all-NSE mode they're available only for stocks in the full
indicator scan: the 800 most active EQ stocks (by expected turnover today) that pass the filters in §18.3. Change the periods with
`EMA_PERIODS` in `config.py`.

### EMA alignment and EMA20 slope (EMA 5m tooltip)

Four separate checks, shown in the tooltip so you can see *which* part of a trend is missing:

| Check | Rule |
|---|---|
| EMA9 > EMA20 | short-term average above the medium one |
| EMA20 > EMA50 | medium above the longer one |
| price > EMA20 | price holding above the 20-candle trend |
| EMA20 rising | EMA20 slope > 0 |

```
EMA20 slope % = (EMA20 now / EMA20 three completed candles ago − 1) × 100
```

**Example:** EMA20 881.57 now and 880.90 three candles ago → slope = (881.57 / 880.90 − 1) × 100 =
**+0.076 %**, so "EMA20 rising: yes". Each check is "n/a" until its EMAs exist.

### Daily EMA set (EMA day column)

The same four spans calculated on **daily closes**, for every stock (daily history is loaded for all of them).

```
yesterday's daily EMA = EMA(daily closes up to the previous session, span)
today's daily EMA     = yesterday's EMA + α × (live price − yesterday's EMA)      α = 2/(span+1)
```

Today's value treats the live price as today's still-forming close, which is what a daily chart
shows during the session. After the close it equals the end-of-day EMA.

| Element | Rule |
|---|---|
| Four dots | live price vs today's daily EMA 9, 20, 50, 200 |
| **stack ↑ / ↓** | price > daily EMA9 > EMA20 > EMA50 > EMA200, or the reverse |
| **50×200 ↑** (golden cross) / **↓** (death cross) | daily EMA50 − EMA200 changed sign within the last 5 sessions, from the daily closes up to yesterday |

**Example:** yesterday's daily EMA9 = 1,480 and the live price is 1,530, so today's
EMA9 = 1,480 + 0.2 × (1,530 − 1,480) = **1,490**. For EMA200, yesterday 1,400 gives
1,400 + 0.00995 × 130 = **1,401.29**.

**History:** an EMA seeded from its first value needs about 3 × span candles before the seed stops
mattering. For the daily EMA200 that's ~600 sessions, so the screener loads **990 days** of daily
candles (~680 sessions). That's still one Dhan request per stock. An EMA shows "—" until a stock has
at least that many daily candles (e.g. EMA200 for a stock listed less than 200 sessions ago).

**How it's used:**

- **Daily EMA vote** (§18.1, `DAILY_EMA_STACK_VOTE`): stack ↑ is a bullish vote, stack ↓ a bearish vote, and mixed gives no vote.
- **Context only:** the dots and the 50×200 tag.

Because daily history exists for every stock, this vote is available for all of them, including
stocks outside the full indicator scan in all-NSE mode.

---

## 6. True Range and ATR

### 6.1 True Range (TR)

```
TR = max( high − low,  |high − previous close|,  |low − previous close| )
```

TR includes gaps that the candle's own high–low range misses.

| Case | Previous close | High | Low | TR |
|---|---|---|---|---|
| Normal candle | 100 | 104 | 101 | max(3, 4, 1) = **4** |
| Gap down | 103 | 98 | 95 | max(3, 5, 8) = **8** |

### 6.2 ATR and ATR % (daily, 14)

```
ATR(14) = Wilder's smoothing of daily TR, period 14
ATR %   = ATR(14) / previous close × 100
```

This uses daily candles **before** the session being screened, from up to 990 days of history.
**Example:** ATR 45, previous close 1,500 → 45 / 1,500 × 100 = **3.0 %**.

**How it's used:** it's a filter. Stocks with ATR % below **1.5 %** move too little intraday to cover
costs, so they go to *Neutral & filtered*. A stock with fewer than 14 daily candles (a recent listing)
has no ATR yet, and its "Why here" text says so.

### 6.3 Intraday ATR % (5-min, 14)

```
intraday ATR   = Wilder's smoothing of 5-min TR, period 14, completed candles
intraday ATR % = intraday ATR / last completed close × 100
```

**Example:** intraday ATR ₹2.65 on a ₹850 stock → **0.31 %** per 5-min candle. It's a guide to how far a
typical candle moves, e.g. for sizing a stop. It's shown in the row tooltip and isn't a filter.

---

## 7. Directional Movement Index: +DI, −DI, ADX

Wilder's DMI, period 14, on 5-min candles. Like most charting tools, it runs continuously across sessions.

### 7.1 Directional movement for each candle

```
up move   = high[t] − high[t−1]
down move = low[t−1] − low[t]

+DM = up move    if up move > down move and up move > 0, else 0
−DM = down move  if down move > up move and down move > 0, else 0
```

| Previous (H, L) | Current (H, L) | Up move | Down move | +DM | −DM |
|---|---|---|---|---|---|
| (105, 100) | (107, 99) | +2 | +1 | **2** | 0 |
| (107, 99) | (104, 97) | −3 | +2 | 0 | **2** |
| (104, 97) | (104, 101) | 0 | −4 | 0 | 0 |

### 7.2 Directional indicators, DX and ADX

```
ATR14  = Wilder(TR, 14)
+DI    = 100 × Wilder(+DM, 14) / ATR14
−DI    = 100 × Wilder(−DM, 14) / ATR14
DX     = 100 × |+DI − −DI| / (+DI + −DI)          (0 when both are 0)
ADX    = Wilder(DX, 14)
```

**Example:** smoothed +DM 1.2, smoothed −DM 0.6, ATR14 4.0:

| Step | Value |
|---|---|
| +DI | 100 × 1.2 / 4.0 = **30** |
| −DI | 100 × 0.6 / 4.0 = **15** |
| DX | 100 × (30 − 15) / (30 + 15) = **33.33** |
| ADX (previous 25) | 25 + (33.33 − 25) / 14 = **25.60** |

**Warm-up.** The first candle has no previous candle, so it gets no DM or TR (Wilder's rule). +DI and
−DI start after 14 more candles, and ADX smooths DX for another 14, so DMI and ADX are published once
there are **28** completed candles.

**ADX rising** = ADX now minus ADX three completed candles ago > 0 (↗), otherwise ↘. A rising ADX means
the trend is getting stronger, whatever its direction.

**How to read it:**

- **+DI > −DI:** up moves are bigger than down moves, so buyers are in control. **−DI > +DI** means sellers are.
- **ADX** measures trend **strength**, not direction. Below 20 the market is usually choppy, 20–25 is a
  developing trend and above 25 (`ADX_STRONG_MIN`, display only) a stronger one. These are initial heuristics to backtest.

**How it's used:**

- **DMI vote:** +DI vs −DI.
- **Strength point:** ADX ≥ **20**.
- **Breakout confirmations:** "DMI" (direction agrees) and "ADX" (≥ 20).
- **Ranking:** ADX breaks ties within the same score.

---

## 8. RSI (14)

Wilder's Relative Strength Index on completed 5-min closes.

```
change[t]  = close[t] − close[t−1]
gain[t]    = change if > 0, else 0           loss[t] = −change if < 0, else 0
avg gain   = Wilder(gain, 14)                avg loss = Wilder(loss, 14)       (§3.2: seeded with a simple average)
RS         = avg gain / avg loss
RSI        = 100 − 100 / (1 + RS)            (100 when avg loss is 0)
RSI slope  = RSI now − RSI three completed candles ago
```

RSI needs 14 changes, so it is published from the **15th** completed candle.

**Example:** Wilder's published example (StockCharts, closes 44.3389 … 46.2820). The first 14 changes
give avg gain **0.2384** and avg loss **0.0996**, so RS = 2.3936 and RSI = 100 − 100 / 3.3936 = **70.53**.
The next close falls by 0.2792: avg gain = (0.2384 × 13 + 0) / 14 = 0.2214, avg loss =
(0.0996 × 13 + 0.2792) / 14 = 0.1124, so RSI = **66.32**. A test checks all 19 published values.

**How to read it** (spec §4.8 heuristics): above 50 is a positive momentum bias, **55–70** the initial
preferred bullish zone, and **above 70** potentially extended, which is not an automatic rejection
(strong trends can stay above 70 for a long time). Above 70 and ≤ 30 are highlighted.

**How it's used:** context only, in the *RSI* column with ↗/↘ for the slope. It isn't a vote.

---

## 9. VWAP

### 9.1 Watchlist (REST) mode: calculated from 5-min candles

```
typical price = (high + low + close) / 3
VWAP          = Σ(typical price × volume) / Σ volume       (today's candles only, from 09:15)
```

**Example:**

| Candle | High | Low | Close | Typical price | Volume | Running VWAP |
|---|---|---|---|---|---|---|
| 09:15 | 101 | 99 | 100 | 100.000 | 1,000 | 100.0000 |
| 09:20 | 103 | 100 | 102 | 101.667 | 3,000 | (100 000 + 305 000) / 4 000 = **101.2500** |
| 09:25 | 102 | 100 | 101 | 101.000 | 2,000 | (405 000 + 202 000) / 6 000 = **101.1667** |

### 9.2 Live feed (WebSocket) mode: the exchange's own number

Dhan's Quote packet carries the **ATP (average trade price)**: total traded value ÷ total traded
volume for the day. That is the exact day VWAP, so in live mode it replaces the candle approximation.

### 9.3 Distance and slope

```
VWAP distance % = (LTP / VWAP − 1) × 100
VWAP slope %    = (VWAP now / VWAP 15 minutes earlier − 1) × 100
```

In REST mode, "now" is the end of the latest completed candle, and "15 minutes earlier" is the VWAP of the
last candle that ended by then. In live mode the exchange VWAP (ATP) is sampled once a minute, and
"15 minutes earlier" is the sample from that minute. The slope is unavailable for the first 15 minutes.

**Example:** VWAP 1,251.20 at 10:30 and 1,249.70 at 10:15 → slope = **+0.12 %** (↗). Price 1,262.40
→ distance = (1,262.40 / 1,251.20 − 1) × 100 = **+0.90 %**.

### 9.4 VWAP reclaim and loss

A confirmed cross, so a price hovering around VWAP doesn't flip back and forth:

```
above  = price > VWAP × (1 + 0.1 %)       below = price < VWAP × (1 − 0.1 %)       otherwise: no change
first reading of the day sets the side (no event)
a move to the other side starts a 60-second timer; going back cancels it
after 60 s still on the new side → event: "reclaim" (now above) or "loss" (now below), timed at the move
```

**Example:** VWAP 100.00. At 10:00 price 101.00 (side: above). 10:01 price 99.95: inside the buffer, ignored.
10:02 price 99.50: below, timer starts. 10:02:59 still below: waiting. 10:03:00 still below: **loss at
10:02**. The tag shows "lost" and stays until the next confirmed cross. The state resets each session.

**How it's used:**

- **VWAP vote:** price above VWAP is bullish, below is bearish. (The vote uses the plain comparison; the
  reclaim/loss tag is context.)
- **Breakout confirmation:** price on the breakout side of VWAP.

---

## 10. Opening range (first 15 minutes)

```
range candles  = today's candles that start before 09:15 + 15 min = 09:30
                 (with 5-min candles: 09:15, 09:20, 09:25)
OR high        = highest high of those candles
OR low         = lowest low of those candles
complete       = now ≥ 09:30, or a candle starting at/after 09:30 exists
```

**Example:**

| Candle | High | Low |
|---|---|---|
| 09:15 | 105 | 100 |
| 09:20 | 106 | 102 |
| 09:25 | 104 | 101 |

This gives **OR high = 106** and **OR low = 100**, complete from 09:30. At 09:27 the same values
show as "forming" and aren't used.

**How it's used:**

- **ORB vote:** price above the OR high is bullish, below the OR low is bearish, and inside it there's no vote.
- **Breakout levels:** *OR High* and *OR Low* (§20), only once the range is complete.

---

## 11. Daily levels: PDH, PDL, PDC, 52-week high/low, pivots and CPR

These come from daily candles **strictly before** the session day:

| Value | Calculation |
|---|---|
| **PDH**, previous day high | high of the last daily candle before today |
| **PDL**, previous day low | low of that candle |
| **PDC**, previous day close | close of that candle |
| **52W High / Low** | highest high and lowest low of the daily candles in the 365 days up to and including the previous session |

**How they're used:**

- **Breakout levels:** PDH and 52W High are upside levels; PDL and 52W Low are downside levels.
- **PDC** is the base for change % and gap %.
- **Hover:** the row tooltip shows all of them.

### 11.1 Floor pivots and the Central Pivot Range (CPR)

From the previous session's high (H), low (L) and close (C):

```
pivot P = (H + L + C) / 3
BC      = (H + L) / 2                 TC = 2 × P − BC          (swapped if BC > TC)
CPR width % = (TC − BC) / P × 100
R1 = 2P − L     S1 = 2P − H     R2 = P + (H − L)     S2 = P − (H − L)
```

**Example:** H 110, L 90, C 105 → P = **101.67**, BC = **100.00**, TC = **103.33** (width 3.28 %),
R1 = **113.33**, S1 = **93.33**, R2 = **121.67**, S2 = **81.67**. A narrow CPR often comes before a
trending day, a wide one before a range day. Shown in the row tooltip only.

---

## 12. Change %, gap % and relative strength

```
Change %  = (LTP / PDC − 1) × 100
Gap %     = (today's open / PDC − 1) × 100
RS        = stock Change % − benchmark Change %
benchmark = NIFTY 50 index (Dhan IDX_I / 13): (index LTP / index previous close − 1) × 100
            fallback when the index is unavailable: the median Change % of all scanned stocks
```

**Example:** stock +1.80 %, NIFTY +0.40 % → RS = **+1.40**, so the stock is outperforming the market.

**How it's used:** the **RS vote** is bullish if RS ≥ **+0.2**, bearish if RS ≤ **−0.2**, and
there's no vote in between.

---

### 12.1 Returns over 5 / 15 / 30 / 60 minutes and the day

Every return runs from a **common start time** to **now** (the live price):

```
start(h) = now rounded down to the 5-minute grid − h minutes        h = 5, 15, 30, 60
price at start = close of the last candle that *finished* by start(h), on today's date
return(h) = (live price / price at start − 1) × 100
return(day) = (live price / previous close − 1) × 100
```

The 5-minute grid makes the start time identical for every series: a stock on 1-min candles (live
feed or REST), a stock on 5-min candles, and an index on 1-min candles. If a start time falls before
09:15, or a series has no candle finished by then, that return is **unavailable**, never zero.

**Example** at 10:33:40: the grid time is 10:30, so the 15-minute start is 10:15. A 1-min series uses
the close of its 10:14 candle (which ends at 10:15), and a 5-min series the close of its 10:10 candle
(also ending at 10:15).

### 12.2 Sector strength vs NIFTY 50

```
sector return(h) = NSE sector index return(h)                           (10 sectors with an index)
                 = median of return(h) of the sector's EQ-series stocks
                   with 20-day turnover ≥ ₹10 crore                     (the other 12: "composite")
Sector RS(h)     = sector return(h) − NIFTY 50 return(h)
```

The mapping is in `sector_indices.csv` (versioned; index IDs checked in Dhan's list on 24-Nov-2026).
A composite needs at least 3 stocks with data, otherwise it's unavailable. Sectors are ranked by
day RS. The state label separates a **rising** sector from one that only **falls less than Nifty**:

| Day RS | Day return | State |
|---|---|---|
| > 0 | > 0 | outperforming, rising |
| > 0 | ≤ 0 | outperforming, falling less than Nifty |
| ≤ 0 | > 0 | underperforming, rising less than Nifty |
| ≤ 0 | ≤ 0 | underperforming, falling |

**Example:** NIFTY IT +0.60% and NIFTY 50 +0.40% on the day give IT a Sector RS of **+0.20**.

### 12.3 Stock strength vs its sector

```
vs Sector(h)  = stock return(h) − its sector's return(h)
percentile    = share of the sector's stocks with a lower day vs-Sector value (ties count half), 0–100
```

**Example:** INFY +1.50% while its sector (NIFTY IT) is +0.60% gives vs Sector **+0.90**. If 4 of
the 5 IT stocks are below that, INFY's percentile is **P100**, the best in its sector.

### 12.4 Market breadth

Counts across every stock that has the value: advancing / declining (vs previous close), above VWAP,
above the 5-min EMA20, within 0.1% of the day high / low, and Day RVOL above 2.5×. Each count is shown
with its base ("n of N"), so stocks without data are never counted as failing.

## 13. Average turnover

```
daily turnover        = close × volume                    (rupees)
average turnover (cr) = mean(daily turnover, last 20 sessions) / 1,00,00,000
```

**Example:** close ₹1,500 × 12,00,000 shares = ₹180 crore that day. The average of the last 20 days is
the value shown.

**How it's used:** an optional filter. With `MIN_AVG_TURNOVER_CR` above 0, a stock below it is treated as too
illiquid for intraday and goes to *Neutral & filtered*. The default is **0: no turnover filter**, so thinly
traded stocks are screened too (their buy/sell gaps can be wide). The value is still shown in the row
tooltip, and it orders the stocks for the full-indicator scan in all-NSE mode (most traded first).

---

## 14. Day RVOL and 5m RVOL (time-of-day relative volume)

### 14.1 Day RVOL (cumulative)

"Is today busier than usual so far?" This compares like with like at the same time of day, so
the heavy opening minutes don't distort it.

```
cutoff     = start time of today's latest *completed* 5-min candle
today      = Σ volume of today's completed candles up to the cutoff
usual      = average over the previous 20 sessions of Σ volume up to the same cutoff
             (sessions with zero volume up to the cutoff are ignored)
Day RVOL   = today / usual            unavailable with fewer than 10 such sessions
```

**Example** (cutoff 09:25; only three earlier sessions are shown to keep it short; the real average uses the previous 20):

| Session | 09:15 | 09:20 | 09:25 | Total to 09:25 |
|---|---|---|---|---|
| 19 Nov | 200 | 100 | 100 | 400 |
| 20 Nov | 300 | 200 | 100 | 600 |
| 23 Nov | 250 | 150 | 200 | 600 |
| **24 Nov (today)** | 700 | 500 | 300 | **1,500** |

The usual volume is (400 + 600 + 600) / 3 = 533.3, so **Day RVOL = 1,500 / 533.3 = 2.81×**.

**How it's used:** a strength point (≥ **1.5×**), the "RVOL" breakout confirmation, and the first number
in the *RVOL day · 5m* column (≥ 2.5× is highlighted). The tooltip shows today's volume, the baseline, the
cutoff and the number of sessions.

### 14.2 5m RVOL (same-slot candle)

"Is **this 5-minute candle** busier than the same 5 minutes usually are?"

```
slot        = time of today's latest completed 5-min candle, e.g. 10:15
usual       = average volume of the 10:15 candle over the previous 20 sessions
              (a session that traded but has no 10:15 candle counts as 0 for that slot)
5m RVOL     = latest completed candle's volume / usual       unavailable with fewer than 10 sessions
LIVE        = the same ratio for the candle still forming, labelled LIVE / INCOMPLETE in the tooltip
              (it grows during the candle and is never treated as a completed-candle signal)
```

**Example:** the 10:15 candle traded **42,000** shares. Over the previous 20 sessions the 10:15 candle
averaged **14,000** → 5m RVOL = **3.0×**. Comparing with the same slot matters: the 09:15 candle is
usually several times busier than the 13:00 one, so a plain "last candle vs last 20 candles" ratio
would flag every opening candle.

**How it's used:** the second number in the *RVOL day · 5m* column, highlighted at ≥ 2.0×. Context
only, not a vote.

---

## 15. Bar RVOL (volume-spike relative volume)

The method comes from the reference `intraday_scanner` project (`src/engines/rvol_engine.py`).
It asks: "Is the **latest minute** unusually heavy compared with the minutes just before it?"

```
completed candles = 1-min candles with start + 1 min ≤ now          (a forming candle never counts)
average           = mean volume of the 20 completed candles before the latest one
Bar RVOL          = latest completed candle's volume / average
candle change %   = (close / open − 1) × 100                         (green if ≥ 0, red if < 0)
```

The 20-candle window can reach back into the previous session, as in the reference.

**Example:** the 20 candles 10:10–10:29 average **8,000** shares. The 10:30 candle traded **22,000**,
opening at 1,250 and closing at 1,254:

| Value | Result |
|---|---|
| Bar RVOL | 22,000 / 8,000 = **2.75×** |
| Class | **VERY_HIGH** |
| Candle change | (1,254 / 1,250 − 1) × 100 = **+0.32 %** (green) |
| Above the 2.5× alert threshold? | yes (§21 for the other conditions) |

**Classes** (same bands as the reference):

| Bar RVOL | Class |
|---|---|
| below 0.8 | LOW |
| 0.8 to below 1.2 | NORMAL |
| 1.2 to below 2.0 | HIGH |
| 2.0 or more | VERY_HIGH (highlighted in the column; ⚡ shows above 2.5) |

**Two implementations, one formula:**

- `bar_rvol_series()` (pandas) is used in REST mode.
- `bar_rvol_recent()` (NumPy) runs for ~3,200 stocks every minute in live mode.

A test checks that they produce identical values.

**Day RVOL vs Bar RVOL:**

| | Day RVOL | 5m RVOL | Bar RVOL |
|---|---|---|---|
| Question | Is the whole day busier than usual? | Is this 5-min candle busier than this slot usually is? | Is this minute a sudden burst? |
| Compares with | the same time of day on the previous 20 sessions | the same slot on the previous 20 sessions | the previous 20 minutes |
| Candles | 5-min | 5-min | 1-min |
| Used for | the score | context | ⚡ alerts |

---

## 16. How 1-minute candles are built from the live feed

In all-NSE mode the 1-min candles for Bar RVOL are built from WebSocket Quote packets
(`MinuteBarBuilder` in `screener/live_feed.py`):

```
candle minute = arrival time (IST) rounded down to the minute
open/high/low/close = first / max / min / last traded price that minute
volume        = increase in the day's cumulative volume since the previous packet
first packet after (re)connecting: only records the cumulative volume (adds 0)
cumulative volume goes down (new day / odd packet): baseline reset, adds 0
```

**Example:**

| Arrival | Price | Cumulative volume | Goes to | Adds |
|---|---|---|---|---|
| 10:00:05 | 100.0 | 10,000 | 10:00 candle | 0 (baseline) |
| 10:00:30 | 101.0 | 10,400 | 10:00 candle | 400 |
| 10:00:50 | 99.5 | 10,500 | 10:00 candle | 100 |
| 10:01:02 | 100.2 | 10,900 | 10:01 candle | 400 |

The result is a 10:00 candle with O 100, H 101, L 99.5, C 99.5 and V **500**, and a 10:01 candle
with V **400**.

Two choices differ from a naive builder:

- The 400 shares traded between 10:00:50 and 10:01:02 count in the new candle. The reference
  project drops them.
- After a disconnect, the volume traded while offline is not dumped into one candle, so it never
  creates a fake spike.

---

## 17. Optional confirmation metrics: Bollinger, OBV, MFI

These are on completed 5-min candles and appear in the **row tooltip** only (hover a row). They aren't
votes or confirmations.

### 17.1 Bollinger Bands (20, 2)

```
middle    = SMA(close, 20)              σ = population standard deviation of the same 20 closes
upper     = middle + 2σ                 lower = middle − 2σ
bandwidth = (upper − lower) / middle × 100
%B        = (close − lower) / (upper − lower)        (undefined when the bands have zero width)
squeeze   = bandwidth ≤ the 10th percentile of its last 120 values
```

**Example:** 20 closes from 100 to 113 with mean **106.35** and σ **3.864** → upper 114.08, lower 98.62,
bandwidth **14.53 %**, and the last close 113 has %B **0.93** (near the upper band). A squeeze marks
unusually narrow bands, which often come before a bigger move.

### 17.2 On-balance volume (OBV) trend

```
OBV[t] = OBV[t−1] + volume  if close rose,  − volume  if it fell,  unchanged if flat
trend  = rising / falling / flat by the change in OBV over the last 10 candles
```

**Example:** closes 100, 101, 100.5, 100.5, 102 with volumes 1,000, 1,500, 700, 400, 900 → OBV 0, 1,500,
800, 800, **1,700**. Rising OBV with rising price means volume is behind the move.

### 17.3 Money Flow Index (MFI, 14)

```
typical price  = (high + low + close) / 3          raw money flow = typical price × volume
positive flow  = Σ raw flow of the last 14 candles whose typical price rose
negative flow  = Σ raw flow of those whose typical price fell
MFI            = 100 − 100 / (1 + positive / negative)        (100 when there is no negative flow)
```

**Example** (period 3 to keep it short): typical prices 10, 11, 10.5, 12 with volumes 1,000, 1,200, 800,
1,500 → flows 13,200 (up), 8,400 (down), 18,000 (up) → MFI = 100 − 100 / (1 + 31,200 / 8,400) = **78.79**.
MFI is a volume-weighted RSI: above 80 / below 20 are the usual stretched zones.

---

## 18. Putting it together: votes, score, bias, filters, ranking

### 18.1 Seven directional votes

| Vote | Bullish when | Bearish when | No vote when |
|---|---|---|---|
| VWAP | price > VWAP | price < VWAP | VWAP unknown |
| MACD | MACD line > Signal | MACD line < Signal | not enough data |
| DMI | +DI > −DI | −DI > +DI | not enough data |
| RS | RS ≥ +0.2 | RS ≤ −0.2 | between −0.2 and +0.2 |
| ORB | price > OR high | price < OR low | inside the range, or range not complete |
| EMA stack (5-min) | price > EMA9 > EMA20 > EMA50 > EMA200 | price < EMA9 < EMA20 < EMA50 < EMA200 | EMAs out of order (mixed), or not enough candles |
| EMA stack (daily) | price > daily EMA9 > EMA20 > EMA50 > EMA200 | the reverse | daily EMAs mixed, or fewer than 200 daily candles |

Each EMA vote can be switched off (`EMA_STACK_VOTE`, `DAILY_EMA_STACK_VOTE`). Each one removed
lowers the number of votes and the maximum score by one (7 votes and 9 points by default).

### 18.2 Bias and score

```
bull votes, bear votes = count of bullish / bearish votes (0–7 each)
strength = (ADX ≥ 20 ? 1 : 0) + (Day RVOL ≥ 1.5 ? 1 : 0)

Bullish  if bull votes ≥ 4 and bull votes > bear votes   →  score = bull votes + strength   (max 9)
Bearish  if bear votes ≥ 4 and bear votes > bull votes   →  score = bear votes + strength
Neutral  otherwise                                         →  score = max(bull votes, bear votes)
```

**Example:**

| Check | Result |
|---|---|
| Price vs VWAP | above → bull |
| MACD vs Signal | above → bull |
| +DI vs −DI | 31 vs 18 → bull |
| RS | +1.4 → bull |
| ORB | inside the range → no vote |
| EMA stack (5-min) | price > EMA9 > EMA20 > EMA50 > EMA200 → bull |
| EMA stack (daily) | daily EMAs mixed → no vote |
| ADX | 26 → +1 strength |
| Day RVOL | 1.2× → +0 strength |

That's 5 bull votes against 0 bear, so the stock is **Bullish**, with score 5 + 1 = **6/9**.
With a bullish daily stack too it would score **7/9**. If both EMA sets were mixed, it would still be
Bullish (4 votes), with a score of **5/9**.

### 18.3 Filters (*Neutral & filtered* unless all pass)

| Filter | Rule |
|---|---|
| Daily history | loaded (turnover known), with at least 14 daily candles so ATR exists |
| Liquidity (optional) | average turnover ≥ `MIN_AVG_TURNOVER_CR` (§13); 0 = off, the default |
| Volatility | ATR % ≥ 1.5 (§6) |
| Series | in `INTRADAY_SERIES` (EQ). BE/BZ are trade-for-trade and SM/ST/SZ are SME, so they're tagged and filtered. |
| Price band (optional) | previous close ≥ `MIN_PRICE` and ≤ `MAX_PRICE` (each off at 0, the default) |

These are also the **hard filters** of the strategies (§19): a stock that fails one is not evaluated.

The price band uses the **previous close**, not the live price, so it decides once which stocks are
screened that day: a stock that closed at ₹990 and rallies to ₹1,050 stays in with `MAX_PRICE = 1000`,
and one that closed at ₹1,010 stays out even if it falls below ₹1,000. (The live price is used only
while a stock has no previous close.) Unlike the other filters, the band also applies to alerts:
stocks outside it get no ⚡ volume-spike alerts and no breakout alerts, even with
`BREAKOUT_ALERTS_REQUIRE_FILTERS` off, and in all-NSE mode they don't take full-scan slots
(`DEEP_SCAN_MAX`), which go to the most active stocks inside the band.

### 18.4 Ranking

Within *Bullish* and within *Bearish*, stocks are sorted by **score**, then **ADX**, both highest
first. The first **5** get the **TOP** tag.

---

## 19. Strategies, setup score and candidate lifecycle

The spec's three scanner strategies (§5 of the spec) and its 100-point ranking (§6). Code:
`screener/strategies.py`, `screener/scoring.py`, `screener/candidates.py`; every threshold is in
`STRATEGIES` and `SCORE_*` in `config.py`.

**Order of work:** hard filters first (turnover, ATR, series, and the optional price band `MIN_PRICE` / `MAX_PRICE`; §18.3), then
the three strategies and the setup score for every stock that passes **and** has intraday indicators.
A stock can qualify for more than one strategy. Each strategy result is one of:

| Status | Meaning |
|---|---|
| qualified | every gate passed (gates skipped for missing data are listed) |
| failed | at least one gate failed (its reason codes are listed) |
| blocked | nothing failed, but a gate had no data: see the missing-data policy (§19.4) |
| filtered | failed a hard filter, not evaluated |
| no_data | no intraday indicators (e.g. outside the deep scan in all-NSE mode) |
| disabled | `"enabled": False` in `STRATEGIES` |

All comparisons are strict (`>`), as in the spec: Day RVOL of exactly 2.50× does **not** pass "> 2.5".
The RS gates use the `rs_horizon` set per strategy (default: the day).

### 19.1 Strong Momentum (MOM)

| Code | Gate (default) |
|---|---|
| `CUM_RVOL` | Day (cumulative) RVOL > 2.5× (§14.1) |
| `SECTOR_RS` | sector return − NIFTY 50 return > 0 (§12.2) |
| `STOCK_VS_SECTOR` | stock return − sector return > 0 (§12.3) |
| `MACD_POSITIVE` | MACD line > 0 **and** histogram > 0 (§4) |
| `ABOVE_VWAP` | price on the *confirmed* upper side of VWAP: beyond the 0.1% buffer for 60 s (§9.4), so one tick above VWAP doesn't count |
| `RSI_ABOVE` | RSI > 50 (§8) |
| `ADX_DI` | ADX > 20 and +DI > −DI (§7); the spec calls this a configurable confirmation, so `"adx_gate": False` removes it |

### 19.2 Early Breakout (BRK)

| Code | Gate (default) |
|---|---|
| `CUM_RVOL` | Day RVOL > 2.0× |
| `LEVEL_BREAK` | a **fresh** break of the opening-range high, PDH or a consolidation high (below) |
| `VOLUME_EXPANSION` | 5m RVOL > 2.0× on the break candle **or** the latest completed candle (§14.2) |
| `RSI_OK` | RSI > 50 **or** rising (RSI change over 3 candles > 0) |
| `MACD_IMPROVING` | a bullish MACD cross in the last 3 candles **or** a rising histogram |
| `ADX_RISING` | ADX higher than 3 candles ago (it need not be above 25) |

**A break** is the first *completed* 5-min candle today whose close is above the level by the breakout
buffer (0.05%). It is found with a binary search on the running maximum of today's closes: the first
candle where that maximum exceeds the level is the first close above it. The break is **fresh** if that
candle ended at most 30 minutes ago (`max_age_min`), and it still counts only while the price is above
the level. With several fresh breaks, the most recent is shown in the *Break* column.

- **OR High** counts only once the opening range is complete (09:30), so there is no look-ahead. No
  opening-range candle can close above its own range high, so the earliest possible break is the 09:30 candle.
- **PDH:** a gap-up opening above PDH counts as a break on the first candle.
- **Consolidation high:** for each candle, the window is the 12 completed candles before it (today only).
  It is a tight base when the window's high − low is at most 3 × the intraday ATR (§6.3) at the candle
  before. A close above the window high by the buffer is a break of that base.

**Example:** OR High 100.00, so the break needs a close above 100.05. Today's closes are 99.0, 99.5, 99.8,
100.3, 100.6, so the running maximum first exceeds 100.05 at the 09:30 candle (close 100.3), which ends at
09:35. At 09:50 the break is 15 min old (fresh), price 100.70 is still above 100, and the 09:30 candle's
5m RVOL of 2.6× passes `VOLUME_EXPANSION`. At 10:30 it would be 55 min old: `LEVEL_BREAK` fails.

### 19.3 Sector Leaders (LDR)

| Code | Gate (default) |
|---|---|
| `SECTOR_RS` | sector return − NIFTY 50 return > 0 |
| `STOCK_VS_SECTOR` | stock return − sector return > 0 |
| `SECTOR_TOP_PCT` | in the top 10% of its sector: vs-sector percentile ≥ 90 (§12.3), with at least 3 sector peers that have data |
| `CUM_RVOL` | Day RVOL > 2.5× |
| `ABOVE_VWAP` | confirmed above VWAP, as in Strong Momentum |
| `EMA9_GT_EMA20` | EMA9 > EMA20 (`"ema9_gt_ema20": False` removes it) |

### 19.4 Missing-data policy

The spec says not to reject a stock silently when an indicator is unavailable, but to apply a configured
policy and explain it. A gate with no data is shown as **?**. By default it **blocks** the strategy
(status *blocked*, with the reason, e.g. "sector RS unavailable (Power)"). To treat a gate as optional,
add its code to that strategy's `skip_if_missing`. It is then skipped when its data is missing (and listed
as skipped), but still applies whenever the data exists. A failed gate always wins: a stock with one failed
gate and one missing gate is *failed*.

### 19.5 Setup score (0–100)

Eight components, each mapped to 0…1 by a **bounded** function (clamped, so no extreme value can
dominate) and multiplied by its weight:

| Component | Weight | 0 points at → full points at |
|---|---|---|
| Cumulative RVOL | 20 | 1.0× → 4.0× (linear) |
| Stock vs sector | 20 | ≤ 0 → +2.0 pts (day) |
| Sector vs Nifty | 15 | ≤ 0 → +1.0 pts (day) |
| MACD momentum | 15 | 5-min state 75% + 15-min state 25%; state values: strong bullish 1, early reversal 0.6, weakening 0.5, neutral 0.2, bearish 0 |
| VWAP position and slope | 10 | above VWAP = 60%; slope 0 → +0.2% over 15 min = 40% |
| ADX and direction | 10 | 0 if +DI ≤ −DI; else ADX 15 → 35 (70%) + (+DI − −DI) 0 → 20 (30%) |
| RSI | 5 | ≤ 50: 0 · 50–55: rises to 0.6 · **55–70: 1** · 70–80: falls to 0.6 · > 80: 0.6 (extended, not rejected) |
| EMA alignment | 5 | 1.25 points per true check (EMA9 > EMA20, EMA20 > EMA50, price > EMA20, EMA20 rising) |

```
points       = Σ weight × value                  (components with data only)
denominator  = Σ weight that could be scored     (a component with no data drops out)
setup score  = points / denominator × 100        (half rounds up)
label        = Strong candidate ≥ 80 · Good candidate ≥ 65 · Watchlist ≥ 50 · Low priority below
               "insufficient data" when the denominator is below 70
```

A missing **part** of a component lowers only that component's share: VWAP without a slope can score 6
of 10, the 15-min MACD missing leaves the 5-min state as the whole component, and EMA checks that aren't
available yet drop out at 1.25 points each.

**Worked example** (checked by a test): Day RVOL 2.5× → 0.5 × 20 = **10** · vs sector +1.0 → 0.5 × 20 =
**10** · sector +0.5 → 0.5 × 15 = **7.5** · MACD strong bullish (5-min) and bearish (15-min) → (0.75 × 1 +
0.25 × 0) × 15 = **11.25** · 0.5% above VWAP with slope +0.1% → (0.6 + 0.4 × 0.5) × 10 = **8** · ADX 25,
+DI 30, −DI 20 → (0.7 × 0.5 + 0.3 × 0.5) × 10 = **5** · RSI 60 → **5** · 3 of 4 EMA checks → **3.75**.
Total **60.5 of 100 → 61, Watchlist**. If the stock's sector had no RS data, both sector components
drop out: 43 points of 65 → **66**, but the label is "insufficient data" because fewer than 70 points
could be scored.

**The setup score is a scanner score for ranking setups, not a probability of profit.** It doesn't
decide whether a stock qualifies (the strategy gates do); it orders the candidates. The original vote
score out of 9 (§18) is kept on the *Bullish / Bearish (votes)* tab.

### 19.6 Candidate lifecycle

Each (strategy, stock) pair has its own state:

```
(none) ──qualifies──▶ NEW ──10 min──▶ active
NEW / active ──stops qualifying──▶ weakening ──qualifies again──▶ active        ("requalified")
weakening ──still failing 10 min later (grace period)──▶ removed               (shown greyed 30 min)
removed ──qualifies again──▶ NEW (entries + 1)
```

A switch between "qualifies" and "doesn't" counts only after it has held for **30 s**
(`CANDIDATE_CONFIRM_SECONDS`), and the transition is timed at the moment the switch started. A price
flickering around a threshold therefore doesn't flip the state on every refresh.

**Example:** a stock qualifies at 10:00:00 and still does at 10:00:30, so the event "qualified" is
recorded at 10:00:00 (NEW). It fails one check at 10:01 but passes again at 10:01:12: no event. At 10:11
it is promoted to active. It stops qualifying at 10:20 → weakening (10:20, with the failing codes), passes
again at 10:22 → requalified, fails from 10:30 and doesn't recover → removed at 10:40.

### 19.7 What is saved

In `data/market.sqlite` (live and REST modes; demo keeps it in memory):

| Table | One row per | Holds |
|---|---|---|
| `strategy_config` | distinct settings | the strategy, score and filter settings as JSON; `config_id` = first 12 hex of its SHA-1 |
| `scanner_candidates` | session, strategy, stock | latest lifecycle state, first-qualified time, entries, entry price/score, peak score; restored after a restart |
| `candidate_events` | transition | the audit trail: time, event, price, score, reason codes (duplicates ignored) |
| `scanner_evaluations` | stock and completed 5-min candle | setup score, points, denominator, label, components, and each strategy's status with its failed / missing codes |

Every row records the `config_id` it was evaluated with. Rows older than 60 days
(`HISTORY_RETENTION_DAYS`) are pruned at startup, together with the indicator snapshots (§24.1).

```sql
SELECT ts, strategy, symbol, event, score, reasons FROM candidate_events
WHERE session_day = '2026-11-24' ORDER BY id;
```

---

## 20. Breakout alerts

**Levels watched:**

| Level | Direction | Available |
|---|---|---|
| OR High / OR Low | up / down | from 09:30 |
| PDH / PDL | up / down | all day |
| 52W High / 52W Low | up / down | all day |

**When an alert fires:**

```
trigger (upside)   = level × (1 + 0.05 %)       price must be strictly above it
trigger (downside) = level × (1 − 0.05 %)       price must be strictly below it
fires when price moves from "not beyond" to "beyond" the trigger
```

- **Once per stock, per level, per day.** A later re-cross of the same level doesn't alert again.
- **Nothing at startup.** A stock already beyond a level when the app starts doesn't alert.
  The exception is a level that first appears mid-session, such as the opening range at 09:30:
  that alerts if price is already beyond it.
- **Filtered stocks only.** Only stocks that pass the filters (§18.3) can alert
  (`BREAKOUT_ALERTS_REQUIRE_FILTERS`).
- **Severity** (§22): HIGH when confirmed, MEDIUM otherwise.

**Example:** PDH = 1,520.00, so the trigger is 1,520 × 1.0005 = **1,520.76**. Price goes
1,519.90 → 1,520.50 (no alert, within the buffer) → 1,521.10 (**alert: broke above PDH**).

**Confirmations** (shown as ✓/✗ on the alert):

| Check | Upside breakout | Downside breakout |
|---|---|---|
| MACD | histogram > 0 | histogram < 0 |
| DMI | +DI > −DI | −DI > +DI |
| ADX | ADX ≥ 20 | ADX ≥ 20 |
| RVOL | Day RVOL ≥ 1.5× | Day RVOL ≥ 1.5× |
| VWAP | price > VWAP | price < VWAP |

**Confirmed** = at least **4 of 5** agree.

### 20.1 Breakout watch: before the break

Code: `screener/watch.py`. A breakout alert (above) fires *after* a level breaks. Breakout watch looks
for the set-up *before* it, long side only, and **records what happened next**, so you can see how often a
breakout really followed. It reads today's 1-min candles (the completed ones) and the live price.

**Levels** (resistance above the price): OR High (from the 1-min candles when the full scan hasn't computed
it), PDH, 52W High and today's high. A level the price is already through (by the 0.05 % buffer) is skipped.
The **nearest** level within 1.5 % is used; levels within 0.3 % of it form one **zone**, measured from its
lowest level and named after all of them (e.g. *PDH 46.30 / OR High + Day high 46.35*).

```
distance %   = (level / price − 1) × 100
test         = a 1-min candle with  high ≥ level × (1 − 0.15 %)  and  low ≤ level   (it came up to the level from below)
               touching candles one after another are one test; two tests need 2 candles away from the level between them
candle size  = (high − low) / close × 100
squeeze      = mean size of the last 5 completed candles / mean size of the 20 candles before them
volume ratio = mean volume of the last 3 completed candles / mean volume of the 20 candles before them
liquidity    = mean ₹ traded per candle (volume × close) over the 20 candles before the last 5
```

| Signal | Conditions (all) | Alert |
|---|---|---|
| **Coiling** | distance ≤ 1.5 % · tests ≥ 2 · squeeze ≤ 0.5 | `watch_coiling`, LOW |
| **Pressing** | distance ≤ 0.3 % · volume ratio ≥ 1.2 · price > VWAP | `watch_pressing`, MEDIUM |

Both also need: the stock passes the hard filters (§18.3, including the price band), at least 10 candles
before the recent ones (so nothing before about 09:30, or within 15 minutes of the feed connecting), an
average of at least ₹5 lakh traded per candle, and the time between 09:15 and 15:10. Each stock gets at
most one signal of a kind per level (a level within 0.5 % of one already signalled counts as the same, until
that one broke out) and none within 15 minutes of the last of that kind.

**Measuring.** Every signal is saved in the `watch_signals` table. For the next 10 minutes
(`WATCH_OUTCOME_MINUTES`) the watcher tracks the live price and the completed candles:

| Field | Meaning |
|---|---|
| `broke_at` | first time price went above level × (1 + 0.05 %), the breakout alert's own rule (the live price; a spike between two price updates counts from its candle's start) |
| `minutes_to_break` | broke_at − signal time |
| `max_gain_pct`, `max_drop_pct` | highest / lowest price in the 10 minutes, from the signal price |
| `outcome` | `broke`, `no_break`, or `no_data` (the stock stopped updating; left out of the hit rate) |

```
hit rate      = signals that broke within 10 min / measured signals (broke + no_break)
fast hit rate = signals that broke within 5 min / measured signals
```

The dashboard's **Breakout watch** tab shows both for today, the last 7 days and since the start. A hit rate
needs a comparison: in the simulated demo (a random walk) roughly 4 in 10 "coiling" stocks touched the level
within 10 minutes, so a real hit rate near that says the signal adds nothing. Also compare the average worst
move with the average best one.

**Worked example (DWARKESH, 27-Nov-2026).** PDH 46.30, opening range 09:15–09:30 high 46.35.

| Time | High | Low | Close | Size % | Note |
|---|---|---|---|---|---|
| 09:24 | 46.35 | 45.70 | 46.12 | 1.41 | test 1 (high ≥ 46.30 × 0.9985 = 46.23, low ≤ 46.30) |
| 09:25–09:27 | ≤ 46.18 | | | 0.22–0.50 | away from the level |
| 09:28 | 46.34 | 45.97 | 46.00 | 0.80 | test 2 |
| 09:29–09:32 | ≤ 46.00 | | 45.89 at 09:32 | 0.17–0.39 | shrinking |

At **09:33:05** the last 5 completed candles (09:28–09:32) average 0.38 % against 0.78 % for the 13 candles
before them: squeeze 0.49 ≤ 0.5. (The replay's 09:15–09:23 candles are reconstructed at the same scale, so this
ratio is approximate; 09:24 onwards are the real candles.) Price 45.89 is 0.89 % below PDH, tested twice:
**DWARKESH coiling 0.89% below PDH 46.30 / OR High + Day high 46.35: tested 2 times (09:24, 09:28)**.
At 09:37:12 price reached 46.68 > 46.30 × 1.0005 = 46.32, so the signal is a hit after 4.1 minutes, with a
best move of +5.7 % (48.50 at 09:41) and a worst of −0.4 % (45.72 at 09:34) inside the 10 minutes.
Pressing never fired: price was 0.9–1.3 % below the level until the minute it broke, and the volume of
09:34–09:36 was a tenth of normal. (`tests/test_watch.py` replays these candles.)

---

## 21. Volume-spike (Bar RVOL) alerts

Each completed 1-min candle is checked **once**. Every candle completed since the previous refresh
is checked, so a slow refresh can't skip one. A ⚡ alert fires only if **all** of these hold:

| # | Condition | Default |
|---|---|---|
| 1 | Bar RVOL **greater than** the threshold | > 2.5× |
| 2 | Candle starts at or after the start time | `auto` = 09:15 + 20 × 1 min = **09:35** (the first candle whose 20-candle average is entirely from today) |
| 3 | Candle finished recently | within the last 5 min (no stale alerts when the app starts late) |
| 4 | Enough money traded: candle volume × LTP | ≥ ₹25 lakh |
| 5 | No alert for this stock recently | 10-min cooldown per stock |
| 6 | Live mode only: candle starts after the feed connected | the minute the feed connected in is partial, so it's skipped |
| 7 | NSE cash-market stock | always |

The alert's direction is the spike candle's colour (green = close ≥ open). Its confirmations are the
same five as for breakouts (§20), checked in that direction.

**Example:** at 10:31:05 the 10:30 candle completed with Bar RVOL **2.75×**, 22,000 shares at
about ₹1,254 (≈ ₹2.76 crore). It's after 09:35, finished seconds ago, there was no earlier alert in
10 minutes, and it's green, so the alert reads
**⚡ volume spike 2.8× on the 10:30 candle (+0.32 %)**.

---

## 22. Alert centre: rules, severity, deduplication

Code: `screener/alerts.py`; settings: `ALERT_RULES` in `config.py`. Every alert is created **on the
server from a state transition**, never by a browser refresh, and saved in the `alerts` table.

### 22.1 Alert types

| Kind | Fires when | Default severity | Covers |
|---|---|---|---|
| `strategy_qualified` | a candidate's lifecycle records *qualified* (§19.6, after the 30 s confirmation) | HIGH for Strong Momentum and Early Breakout, MEDIUM for Sector Leaders; one level lower when the setup score is below 50 or "insufficient data" | each strategy |
| `strategy_lost` | the candidate stops qualifying (*weakening*); the message names the failed gates | MEDIUM | |
| `strategy_removed` | the grace period ends without requalifying | LOW | |
| `strategy_requalified` | a weakening candidate qualifies again | LOW | |
| `score_cross` | a candidate's setup score rises through 80 (HIGH) or 65 (MEDIUM); only the highest threshold crossed alerts | HIGH / MEDIUM | candidates |
| `rvol_cross` | Day RVOL rises above 2.5× | MEDIUM | every scored stock |
| `rs_positive` | stock vs sector (day) turns positive | MEDIUM | watch |
| `sector_top` | the stock enters the top 10% of its sector (percentile ≥ 90, at least 3 peers) | MEDIUM | watch |
| `sector_rs_positive` | a sector's RS vs NIFTY 50 (day) turns positive | MEDIUM | each sector |
| `macd_bullish` | a completed candle where the histogram goes from ≤ 0 to > 0 (the bullish crossover) | MEDIUM | watch |
| `vwap_cross` | a confirmed VWAP reclaim (up) or loss (down), §9.4 | MEDIUM | watch |
| `breakout` | the level crosses of §20; an opening-range breakout is labelled as such | HIGH when confirmed, MEDIUM otherwise | filter-passing stocks |
| `rvol` | a ⚡ Bar RVOL spike, §21 | MEDIUM | every NSE stock |
| `watch_coiling` | Breakout watch: coiling under a tested level, §20.1 | LOW | filter-passing stocks |
| `watch_pressing` | Breakout watch: pressing a level on rising volume above VWAP, §20.1 | MEDIUM | filter-passing stocks |
| `feed` | prices stale, feed disconnected or stopped for 10 s (*CRITICAL*); back to normal (*LOW*) | CRITICAL / LOW | system |
| `data_error` | a candle / quote / calculation failure (repeats within 10 min fold into one alert with a count and example symbols) | MEDIUM; CRITICAL when the app's own loop fails | system |
| `auth` | Dhan rejects the credentials | CRITICAL | system |

**Scope** keeps 500 stocks from flooding the centre: *candidates* = current candidates of any strategy
(new / active / weakening); *watch* = candidates plus stocks with a setup score of at least 50
(`ALERT_WATCH_MIN_SCORE`); *scored* = every stock that passes the hard filters and has intraday indicators.

### 22.2 Transitions, hysteresis and the first look

Each rule remembers the last state per stock. The **first look** at a stock only records its state, so
starting the app at 11:00 doesn't fire alerts for things that crossed at 10:00. A threshold rule fires on a
rising edge and **re-arms** only after the value falls back below a lower level:

| Rule | Fires above | Re-arms below |
|---|---|---|
| `rvol_cross` | 2.5× | 2.0× |
| `rs_positive`, `sector_rs_positive` | +0.1 | −0.1 |
| `sector_top` | percentile 90 | 80 |
| `score_cross` | 80 / 65 | 75 / 60 (threshold − 5) |

**Example:** Day RVOL 2.0 → 2.6 alerts; 2.7, 2.2, 2.6 don't (it never fell below 2.0); 1.9 then 2.51 alerts
again, unless it is still inside the 30-minute cooldown.

The tracker and the state are reset each session. The state stays updated for stocks outside the scope, so
a stock that enters the watch list with RS already positive doesn't produce a late "turned positive" alert.

### 22.3 Deduplication and cooldown

- **Unique key.** Every alert has a key, e.g. `2026-11-24|strategy_qualified|strong_momentum|SBIN|2026-11-24 10:31:22`
  or `2026-11-24|breakout|SBIN|PDH`. A key already seen today is dropped, in memory and by a unique index in
  SQLite. After a restart, today's keys are reloaded, so nothing fires twice.
- **Cooldown.** At most one alert per kind and stock (or sector) within the rule's `cooldown_min`
  (15–30 min). Cooldowns are reloaded after a restart too.
- **Feed blips.** A bad feed state must last 10 s (`ALERT_RULES["feed"]["after_seconds"]`, real seconds)
  before the CRITICAL alert; the LOW recovery alert is sent only after a CRITICAL one.

### 22.4 What each alert contains

Time (IST), severity, type, symbol, sector, strategy, the message (why it fired, with the values), the
reason codes, and a **values snapshot** taken from the evaluated row at that moment: LTP, change %, Day RVOL,
5m RVOL, stock vs sector, sector RS, RS vs Nifty, MACD histogram and state, RSI, ADX, +DI, −DI, VWAP and its
distance and slope, setup score and label, sector percentile. Example:

```
[HIGH] 10:35:00 IST | ABC | Strong Momentum | Strong Momentum: all gates passed. Cumulative RVOL 2.8x;
Sector RS +0.42; Stock vs sector +0.76; MACD histogram positive (strong bullish); RSI 63; price 0.35% above VWAP;
setup score 78 (Good candidate).
```

**Severity is an operational priority, not a prediction of trading success.** Alerts are informational
and need your own check; they are not buy or sell instructions, and the app never places orders.

### 22.5 Acknowledgement and history

Each alert can be acknowledged (`acknowledged_at` in SQLite). The dashboard filters by severity, type
(setups / system), strategy, symbol and date, and "Acknowledge shown" acknowledges everything the filters
match. Other days come from the database (`GET /api/alerts?day=YYYY-MM-DD`). `POST /api/alerts/ack`
accepts JSON only, so another web page can't acknowledge alerts through your browser. From the terminal:
`history.py alerts --severity HIGH CRITICAL --unacked`.

### 22.6 Toasts, sound and notifications (dashboard)

What *pops up* is a per-browser choice in **Alert settings**: toasts from a minimum severity (MEDIUM by
default) and per type (on by default for qualified candidates, score crossings, breakouts, volume spikes
and system alerts), a sound per type (**off until you switch it on**, because browsers block autoplay),
and desktop notifications while the tab is in the background, after you grant permission (HIGH and above
by default). CRITICAL and HIGH toasts stay until dismissed or acknowledged; others disappear after 12 s.
Rows of stocks with a new alert are highlighted for 2 minutes. The centre always keeps every alert.

---

## 23. Backtesting: replay, trades, costs, metrics

Code: `screener/backtest.py`, `backtest.py`; settings: `BACKTEST` in `config.py`. A simulation on past
data, never orders.

### 23.1 Replay without look-ahead

1. Each stock's 5-min candles become feature arrays in which the value at candle *i* uses only candles
   up to *i*: the EMAs, MACD, RSI, DMI/ADX and ATR are causal recursions; VWAP, the opening range and the
   running maximum of closes restart each session and only grow; cumulative and same-slot RVOL compare
   with **earlier** sessions only; consolidation breaks look only at the 12 candles before; the 15-min
   MACD uses 15-min candles that had **finished** by the close. Daily levels and the hard filters use
   daily candles before the session.
2. At every candle close *T* the row the live engine would have had is rebuilt, and the same code runs on
   it: horizon returns and sector strength (§12), the VWAP reclaim/loss state machine (§9.4), EMA
   alignment, the hard filters (§18.3), the three strategies (§19) and the setup score.
3. A test compares the rebuilt values with the live `compute_intraday` at sample closes (they are
   identical), and another cuts every candle after 12:00 and checks that every decision before 12:00 is
   unchanged.

| Live dashboard | Replay |
|---|---|
| reacts to every price update | decides at 5-min candle closes, price = the close |
| candidate change must hold 30 s | holding at the close counts |
| exchange VWAP in live-feed mode | VWAP from the candles (§9.1) |
| today's universe and sectors | the same lists for the whole period (survivorship bias) |

### 23.2 Trades

```
signal  = a strategy starts qualifying at the close of candle i
entry   = open of candle i+1 × (1 + slippage)             (no entry after 14:45; 1 per strategy, stock, day)
stop    = entry − stop_atr_mult × 5-min ATR at the signal  (or entry × (1 − stop_pct), or none)
target  = entry + target_r × (entry − stop)                (optional)
exit    = first of: stop (low ≤ stop; the open if it gapped below), target (high ≥ target),
          N closes without qualifying (optional, at the next open), the candle starting at 15:15 (its open)
          × (1 − slippage); stop beats target inside the same candle
```

**Example** (slippage 0 for clarity): signal at the 09:20 candle's close, entry at the 09:25 candle's
open ₹100.00, ATR ₹1.00, stop 1.5 × ATR = ₹98.50. The 09:30 candle trades down to ₹98.00, so the trade
exits at ₹98.50: return −1.5%, **R = −1.0**, MAE −2.0% (the candle's low). 1,000 shares at
₹1,00,000 per trade.

**MFE / MAE** are the highest high and lowest low from the entry candle through the exit candle
(excluding a candle exited at its open), relative to the entry: how far the trade went for and
against you. **R** = (exit − entry) / (entry − stop).

### 23.3 Costs

Per round trip (defaults in `BACKTEST["costs"]`, approximate Indian intraday equity charges at the
time of writing; check your broker's charge sheet):

```
brokerage = min(₹20, 0.03% × order value)       per order (buy and sell)
exchange  = 0.00297% × (buy + sell)             SEBI = ₹10 per crore × (buy + sell)
STT       = 0.025% × sell                       stamp duty = 0.003% × buy
GST       = 18% × (brokerage + exchange + SEBI)
```

**Example:** buy ₹1,00,000, sell ₹1,00,500 → brokerage 20 + 20, exchange 5.95, SEBI 0.20, STT 25.13,
stamp 3.00, GST 8.31 → **₹82.59**, about 0.08% of the trade. Slippage (3 bps per side by default) is
applied to the prices on top of that.

### 23.4 Metrics

| Metric | Definition |
|---|---|
| Win rate | share of trades with net P&L > 0 |
| Average net return | mean of net P&L ÷ (quantity × entry), with a 95% interval: mean ± 1.96 × sd / √n |
| Expectancy | mean net P&L in ₹ per trade |
| Profit factor | total net wins ÷ total net losses |
| Max drawdown | largest fall of cumulative net P&L from its peak, trades in exit order; also as % of `starting_equity` |
| Breakdowns | the same metrics by entry time (hour buckets), setup score at the signal, sector and exit reason |

Fewer than **30** trades are flagged (⚠). An interval that includes 0 means the test hasn't shown an edge.

### 23.5 In-sample, out-of-sample and variants

The test sessions are split in time: the first 70% (`in_sample_fraction`) are in-sample, the rest
out-of-sample. A **variant** is a set of settings-page changes (`--variant "name: KEY=VALUE; …"` or a
JSON file), validated like the settings page. Every variant runs on the same sessions; features are
reused unless the variant changes an indicator setting. Choose on in-sample results, judge on
out-of-sample ones: a variant much better in-sample than out-of-sample was probably fitted to noise.

---

## 24. Data windows and warm-up

| Data | Window loaded | Why |
|---|---|---|
| 5-min candles | 35 calendar days (about 23–25 sessions, ~1,800 candles) | lets EMA and Wilder's smoothing settle (EMA200 needs at least 200 candles); Day RVOL baseline of 20 previous sessions, with room for holidays |
| 1-min candles (REST mode) | 5 calendar days | 20-candle Bar RVOL window, covering weekends |
| 1-min candles (live mode) | built since the feed connected (last 120 kept) | Bar RVOL starts once 21 completed candles exist, ~21 min after connecting |
| Daily candles | 990 days (~680 sessions) | daily EMA200 settled, 52-week levels, 14-day ATR, 20-day turnover |

All of it is cached in `data/market.sqlite`, so later runs only fetch new candles.

### 24.0 The detail-panel chart

The stock detail panel's chart (`/api/chart/SYMBOL?interval=…`) is calculated on the server with the
same functions as the tables, on **completed** candles, over a warm-up window, then cut to the sessions
shown. The still-forming candle is drawn faded and has no indicator values.

| Candles | Loaded (calendar days) | Shown | Series |
|---|---|---|---|
| 1-min | 4 | today | EMA 9, EMA 20, session VWAP, MACD (12, 26, 9), RSI 14, +DI / −DI / ADX 14 |
| 5-min | 12 | the last 2 sessions | same |
| 15-min (from 5-min, 09:15-aligned) | 40 | the last 5 sessions | same |

The chart's VWAP restarts each session and uses candle typical prices (§9.1). In live-feed mode the
tables use the exchange's VWAP (§9.2), so the two can differ slightly; the panel says which is which.

### 24.1 Indicator snapshots

Each time a stock's 5-min candle completes, its indicator values are saved once to the
`indicator_snapshots` table (live and REST modes, not demo): RSI, MACD line/signal/histogram/state,
ADX, +DI, −DI, EMA 9/20/50/200, VWAP, intraday ATR %, Day RVOL, 5m RVOL, Bollinger bandwidth and MFI,
keyed by symbol, interval and candle start time. This is for reviewing a day afterwards and for
backtesting later. Example query:

```sql
SELECT bar_ts, rsi, macd_state, adx, rvol FROM indicator_snapshots
WHERE symbol = 'INFY' AND session_day = '2026-11-24' ORDER BY bar_ts;
```

Switch it off with `SAVE_INDICATOR_SNAPSHOTS = False`.

---

## 25. Why values can differ from TradingView or your broker's chart

1. **Seeding.** EMAs start from the first value; Wilder's smoothing (RSI, ATR, DMI) starts from a
   simple average of the first *period* values, as TradingView and StockCharts do. Both run over the
   whole ~1,800-candle warm-up, so a platform that loads a different amount of history can still show
   slightly different values for long periods such as EMA200.
2. **VWAP.** In REST mode, VWAP is approximated from 5-min typical prices. The exchange's VWAP (shown
   in live mode, and on most broker charts) uses every trade, so expect small differences.
3. **Forming candle.** The screener ignores the forming candle for every momentum and volume indicator,
   so its MACD, RSI or ADX match the chart's value for the *previous* candle until the current one closes.
   Charts update the last candle live.
4. **RVOL definitions vary.** Platforms define "RVOL" differently: per candle, same slot, cumulative to
   time of day, or against the 50-day average volume. Check which one you're comparing with (§14 vs §15).
5. **Session gaps.** MACD and DMI run across sessions, so the first candle of a day includes the
   overnight gap in its TR and DM. Most charting platforms do the same.
6. **Data source.** Candle values come from Dhan. Another broker's feed can differ slightly.

---

## 26. All settings

All in `config.py`:

| Setting | Default | Meaning |
|---|---|---|
| `CANDLE_INTERVAL_MIN` | 5 | candle size for MACD, DMI, VWAP, OR and Day RVOL |
| `MACD_FAST`, `MACD_SLOW`, `MACD_SIGNAL` | 12, 26, 9 | MACD periods |
| `MACD_CROSS_LOOKBACK_BARS` | 3 | candles checked for a fresh MACD cross |
| `EMA_PERIODS` | (9, 20, 50, 200) | trend EMAs shown in the EMA column |
| `EMA_CROSS_FAST`, `EMA_CROSS_SLOW` | 9, 20 | EMAs used for the 9×20 crossover tag |
| `EMA_CROSS_LOOKBACK_BARS` | 3 | candles checked for a fresh 9×20 cross |
| `DMI_PERIOD` | 14 | +DI, −DI, ADX period |
| `ADX_TREND_MIN` | 20 | ADX strength point and confirmation |
| `DAILY_ATR_PERIOD` | 14 | daily ATR period |
| `MIN_ATR_PCT` | 1.5 | volatility filter (%) |
| `AVG_TURNOVER_DAYS` | 20 | turnover average window |
| `MIN_AVG_TURNOVER_CR` | 0 (off) | liquidity filter (₹ crore); 0 = no turnover filter |
| `OPENING_RANGE_MIN` | 15 | opening range length |
| `RS_MIN_PCT` | 0.2 | relative-strength vote threshold |
| `RVOL_LOOKBACK_SESSIONS` | 20 | Day RVOL baseline sessions |
| `RVOL_MIN` | 1.5 | Day RVOL strength point and confirmation |
| `MIN_DIRECTIONAL_VOTES` | 4 | votes needed (of 7) for bullish/bearish |
| `EMA_STACK_VOTE` | True | count the 5-min EMA stack as a vote |
| `TOP_N` | 5 | stocks tagged TOP per list |
| `BREAKOUT_BUFFER_PCT` | 0.05 | how far price must clear a level (%) |
| `MIN_CONFIRMATIONS` | 4 | confirmations (of 5) for "confirmed" |
| `BREAKOUT_ALERTS_REQUIRE_FILTERS` | True | breakout alerts only for filter-passing stocks |
| `RVOL_SPIKE_INTERVAL_MIN` | 1 | Bar RVOL candle size |
| `RVOL_SPIKE_LOOKBACK_BARS` | 20 | Bar RVOL average window |
| `RVOL_ALERT_THRESHOLD` | 2.5 | ⚡ alert when Bar RVOL is above this |
| `RVOL_ALERT_FROM` | auto (09:35) | earliest candle that may alert |
| `RVOL_ALERT_COOLDOWN_MIN` | 10 | minutes between ⚡ alerts per stock |
| `RVOL_ALERT_MAX_AGE_MIN` | 5 | ignore candles that finished longer ago |
| `RVOL_ALERT_MIN_BAR_VALUE` | 25,00,000 | minimum ₹ traded in the spike candle |
| `INTRADAY_SERIES` | ("EQ",) | series that get the full scan, lists and breakout alerts |
| `WARMUP_CALENDAR_DAYS` | 35 | 5-min history loaded (warm-up + 20-session Day RVOL baseline) |
| `DAILY_HISTORY_DAYS` | 990 | daily history loaded (one request per stock; settles the daily EMA200) |
| `DAILY_EMA_PERIODS` | (9, 20, 50, 200) | daily EMAs in the EMA day column |
| `DAILY_EMA_CROSS_FAST`, `DAILY_EMA_CROSS_SLOW` | 50, 200 | golden / death cross tag |
| `DAILY_EMA_CROSS_LOOKBACK_DAYS` | 5 | sessions checked for a fresh 50×200 cross |
| `DAILY_EMA_STACK_VOTE` | True | count the daily EMA stack as a vote |
| `SECTOR_INDEX_FILE` | sector_indices.csv | NSE sector → sector index, or composite |
| `SECTOR_COMPOSITE_MIN_TURNOVER_CR` | 10 | composite uses EQ stocks above this 20-day turnover (₹ crore) |
| `SECTOR_COMPOSITE_MIN_MEMBERS` | 3 | fewer stocks with data → sector return unavailable |
| `BREADTH_RVOL_THRESHOLD` | 2.5 | breadth counts stocks with Day RVOL above this |
| `STALE_FEED_SECONDS` | 30 | no price update for this long while open → DATA STALE |
| `RSI_PERIOD`, `RSI_SLOPE_BARS` | 14, 3 | RSI period and the candles its slope covers |
| `ADX_SLOPE_BARS` | 3 | "ADX rising" compares with this many candles ago |
| `ADX_STRONG_MIN` | 25 | "stronger trend" label in the ADX tooltip (display only) |
| `EMA_SLOPE_BARS` | 3 | EMA20 slope window (candles) |
| `MACD_CONTEXT_INTERVAL_MIN` | 15 | candle size for the MACD context state |
| `INTRADAY_ATR_PERIOD` | 14 | ATR on 5-min candles (tooltip) |
| `VWAP_SLOPE_MINUTES` | 15 | VWAP slope window |
| `VWAP_CROSS_BUFFER_PCT`, `VWAP_CONFIRM_SECONDS` | 0.1, 60 | VWAP reclaim/loss: how far beyond VWAP, and for how long |
| `RVOL_MIN_SESSIONS` | 10 | fewer past sessions → Day RVOL and 5m RVOL unavailable |
| `CANDLE_RVOL_THRESHOLD` | 2.0 | 5m RVOL highlight |
| `CUM_RVOL_THRESHOLD` | 2.5 | Day RVOL highlight |
| `BB_PERIOD`, `BB_STD` | 20, 2.0 | Bollinger Bands |
| `BB_SQUEEZE_LOOKBACK`, `BB_SQUEEZE_PCTL` | 120, 0.10 | squeeze = bandwidth in its lowest 10% of 120 candles |
| `MFI_PERIOD` | 14 | Money Flow Index |
| `OBV_TREND_BARS` | 10 | OBV trend window |
| `SAVE_INDICATOR_SNAPSHOTS` | True | save each completed 5-min evaluation to SQLite |
| `STRATEGIES` | see §19.1–19.3 | per strategy: `enabled`, thresholds, `rs_horizon`, `skip_if_missing` |
| `MIN_PRICE`, `MAX_PRICE` | 0, 0 (off) | optional price band on the previous close (₹); also silences alerts outside it (§18.3) |
| `CONSOLIDATION_BARS`, `CONSOLIDATION_MAX_ATR_MULT` | 12, 3.0 | consolidation-high window and how tight it must be |
| `SCORE_WEIGHTS` | 20/20/15/15/10/10/5/5 | setup-score component weights |
| `SCORE_BOUNDS` | see §19.5 | where each component scores 0 and full points |
| `SCORE_RS_HORIZON` | day | RS horizon used by the score |
| `SCORE_MIN_DENOMINATOR` | 70 | fewer scorable points → "insufficient data" |
| `SCORE_LABELS` | 80 / 65 / 50 / 0 | Strong / Good / Watchlist / Low priority |
| `CANDIDATE_CONFIRM_SECONDS` | 30 | a qualify / stop change must hold this long |
| `CANDIDATE_NEW_MINUTES` | 10 | NEW badge duration |
| `CANDIDATE_GRACE_MINUTES` | 10 | weakening this long → removed |
| `CANDIDATE_REMOVED_SHOW_MINUTES` | 30 | removed candidates stay visible (greyed) |
| `SAVE_SCANNER_EVALUATIONS` | True | one row per stock per completed candle in `scanner_evaluations` |
| `HISTORY_RETENTION_DAYS` | 60 | prune snapshots, evaluations and candidate history older than this |
| `ALERT_RULES` | see §22.1 | per alert type: `enabled`, severity, thresholds, re-arm levels, scope, cooldown |
| `ALERT_WATCH_MIN_SCORE` | 50 | setup score that puts a stock in the "watch" scope |
| `WATCH_ENABLED` | True | Breakout watch on (§20.1) |
| `WATCH_MAX_DIST_PCT`, `WATCH_TOUCH_PCT`, `WATCH_MIN_TESTS` | 1.5, 0.15, 2 | coiling: how far below the level, what counts as a test, tests needed |
| `WATCH_SQUEEZE_BARS`, `WATCH_SQUEEZE_RATIO` | 5, 0.5 | coiling: recent candles and how small they must be vs the 20 before |
| `WATCH_PRESS_DIST_PCT`, `WATCH_PRESS_BARS`, `WATCH_PRESS_VOLUME` | 0.3, 3, 1.2 | pressing: distance, recent candles, volume vs the 20 before |
| `WATCH_MIN_AVG_BAR_VALUE` | 5,00,000 | minimum average ₹ per 1-min candle |
| `WATCH_COOLDOWN_MIN` | 15 | one signal of a kind per stock in this window |
| `WATCH_OUTCOME_MINUTES` | 10 | a hit = the level breaks within this |
| `WATCH_UNTIL` | 15:10 | no new signals after this |
| `ALERT_MEMORY`, `ALERT_SNAPSHOT_MAX` | 1000, 200 | alerts kept in memory / sent to a newly opened page |
| `BACKTEST` | see §23 | capital per trade, last entry, exit time, stop / target / lost-eligibility exits, trades per day, in-sample share, costs |
| `PUSH_INTERVAL_SECONDS` | 1.0 | how often the dashboard WebSocket sends changes |
