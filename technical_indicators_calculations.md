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
8. [VWAP](#8-vwap)
9. [Opening range (first 15 minutes)](#9-opening-range-first-15-minutes)
10. [Daily levels: PDH, PDL, PDC, 52-week high/low](#10-daily-levels-pdh-pdl-pdc-52-week-highlow)
11. [Change %, gap % and relative strength](#11-change--gap--and-relative-strength)
12. [Average turnover](#12-average-turnover)
13. [Day RVOL (time-of-day relative volume)](#13-day-rvol-time-of-day-relative-volume)
14. [Bar RVOL (volume-spike relative volume)](#14-bar-rvol-volume-spike-relative-volume)
15. [How 1-minute candles are built from the live feed](#15-how-1-minute-candles-are-built-from-the-live-feed)
16. [Putting it together: votes, score, bias, filters, ranking](#16-putting-it-together-votes-score-bias-filters-ranking)
17. [Breakout alerts](#17-breakout-alerts)
18. [Volume-spike (Bar RVOL) alerts](#18-volume-spike-bar-rvol-alerts)
19. [Data windows and warm-up](#19-data-windows-and-warm-up)
20. [Why values can differ from TradingView or your broker's chart](#20-why-values-can-differ-from-tradingview-or-your-brokers-chart)
21. [All settings](#21-all-settings)

---

## 1. Conventions

| Item | Convention |
|---|---|
| Time zone | All timestamps are IST (Asia/Kolkata). |
| Candle timestamp | The candle's **start** time. The `09:15` 5-min candle covers 09:15:00–09:19:59. |
| Session | NSE cash market, 09:15–15:30. Holidays are not detected. |
| "Session day" | From 09:00 on a weekday it's **today**, even before the first candle exists. Otherwise it's the latest trading day in the data. So at 09:05, PDH means *yesterday's* high. |
| Indicator candles | **5-minute** candles for MACD, EMA 9/20/50/200, DMI/ADX, VWAP (REST mode), opening range and Day RVOL. **1-minute** candles for Bar RVOL. **Daily** candles for ATR, turnover and price levels. |
| "Completed" candle | A candle is complete once `now ≥ start + interval`. Bar RVOL only uses completed candles. |

**Where the data comes from:**

| Value | Watchlist mode (REST) | All-NSE mode (`--universe nse`, WebSocket) |
|---|---|---|
| Last traded price (LTP) | REST LTP quote every 5 s | WebSocket Quote packet (real time) |
| VWAP | Calculated from 5-min candles (§8) | Dhan's **ATP** (average trade price) from the feed |
| 1-min candles (Bar RVOL) | REST 1-min candles | Built from the live ticks (§15) |
| 5-min candles (MACD, DMI, OR, Day RVOL) | REST, every 60 s | REST, once per 5-min bar, for the most liquid EQ stocks only |
| Daily candles | REST, once per day | REST, once per day, for every stock |
| Previous close | Daily candles | Daily candles, or the feed's Prev Close packet until those load |

---

## 2. Quick reference

| Indicator | Candles | Default settings | Used for | Code |
|---|---|---|---|---|
| EMA | — | span *n*, α = 2/(n+1) | inside MACD | `ema()` |
| Wilder's smoothing (RMA) | — | α = 1/period | inside ATR, DMI, ADX | `rma()` |
| MACD line, signal, histogram | 5-min | 12, 26, 9 | MACD vote, breakout confirmation, cross tag | `macd()` |
| MACD cross | 5-min | last 3 bars | "× ↑ / × ↓" tag | `recent_macd_cross()` |
| EMA 9 / 20 / 50 / 200 | 5-min | spans 9, 20, 50, 200 | EMA vote (stack), EMA column: dots, stack, 9×20 cross | `ema_values()`, `ema_stack()`, `recent_cross()` |
| True Range | daily, 5-min | — | inside ATR and DMI | `true_range()` |
| ATR % | daily | 14 | filter (≥ 1.5%) | `atr()` → `compute_daily_stats()` |
| +DI / −DI | 5-min | 14 | DMI vote, confirmation | `dmi()` |
| ADX | 5-min | 14 | strength point (≥ 20), confirmation | `dmi()` |
| VWAP | 5-min / live | session-anchored | VWAP vote, confirmation | `session_vwap()` or feed ATP |
| Opening range high/low | 5-min | first 15 min | ORB vote, breakout levels | `opening_range()` |
| PDH / PDL / PDC | daily | previous session | breakout levels, change % | `compute_daily_stats()` |
| 52-week high/low | daily | 365 days | breakout levels | `compute_daily_stats()` |
| Change %, Gap % | live + daily | — | display, relative strength | `_base_rows()` |
| Relative strength (RS) | live | vs NIFTY 50, ±0.2 | RS vote | `_finish_rows()` |
| Average turnover | daily | 20 days | filter (≥ ₹100 cr) | `compute_daily_stats()` |
| Day RVOL | 5-min | previous 20 sessions | strength point (≥ 1.5×), confirmation | `relative_volume()` |
| Bar RVOL | 1-min | 20 candles | ⚡ alert (> 2.5×), Bar RVOL column | `bar_rvol_series()`, `bar_rvol_recent()` |

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

The code uses `pandas.ewm(alpha=1/period, adjust=False)`. **Example** (period 14): previous RMA 2.0, new value 3.4
→ 2.0 + (3.4 − 2.0) / 14 = **2.1**.

---

## 4. MACD (12, 26, 9)

Calculated on 5-min closes, including the warm-up history from earlier sessions (§19).

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

**What the *EMA 9·20·50·200* column shows:**

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

- **EMA vote** (§16.1, `EMA_STACK_VOTE`): stack ↑ is a bullish vote, stack ↓ a bearish vote, and mixed gives no vote.
- **Context only:** the individual dots and the 9×20 tag aren't part of the score, and the EMAs aren't a breakout confirmation. In all-NSE mode they're available only for stocks in the full
indicator scan: the most liquid EQ stocks that pass the filters in §16.3. Change the periods with
`EMA_PERIODS` in `config.py`.

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

This uses daily candles **before** the session being screened, from up to 400 days of history.
**Example:** ATR 45, previous close 1,500 → 45 / 1,500 × 100 = **3.0 %**.

**How it's used:** it's a filter. Stocks with ATR % below **1.5 %** move too little intraday to cover
costs, so they go to *Neutral & filtered*.

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

**How to read it:**

- **+DI > −DI:** up moves are bigger than down moves, so buyers are in control. **−DI > +DI** means sellers are.
- **ADX** measures trend **strength**, not direction. Below about 20 the market is usually choppy; above 20–25 it's trending.

**How it's used:**

- **DMI vote:** +DI vs −DI.
- **Strength point:** ADX ≥ **20**.
- **Breakout confirmations:** "DMI" (direction agrees) and "ADX" (≥ 20).
- **Ranking:** ADX breaks ties within the same score.

---

## 8. VWAP

### 8.1 Watchlist (REST) mode: calculated from 5-min candles

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

### 8.2 Live feed (WebSocket) mode: the exchange's own number

Dhan's Quote packet carries the **ATP (average trade price)**: total traded value ÷ total traded
volume for the day. That is the exact day VWAP, so in live mode it replaces the candle approximation.

**How it's used:**

- **VWAP vote:** price above VWAP is bullish, below is bearish.
- **Breakout confirmation:** price on the breakout side of VWAP.

---

## 9. Opening range (first 15 minutes)

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
- **Breakout levels:** *OR High* and *OR Low* (§17), only once the range is complete.

---

## 10. Daily levels: PDH, PDL, PDC, 52-week high/low

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

---

## 11. Change %, gap % and relative strength

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

## 12. Average turnover

```
daily turnover        = close × volume                    (rupees)
average turnover (cr) = mean(daily turnover, last 20 sessions) / 1,00,00,000
```

**Example:** close ₹1,500 × 12,00,000 shares = ₹180 crore that day. The average of the last 20 days is
the value shown.

**How it's used:** it's a filter. Below **₹100 crore** a stock is too illiquid for intraday and goes to *Neutral & filtered*.

---

## 13. Day RVOL (time-of-day relative volume)

"Is today busier than usual so far?" This compares like with like at the same time of day, so
the heavy opening minutes don't distort it.

```
cutoff     = start time of today's latest 5-min candle
today      = Σ volume of today's candles up to the cutoff
usual      = average over the previous 20 sessions of Σ volume up to the same cutoff
             (sessions with zero volume are ignored; a stock with less history uses what it has)
Day RVOL   = today / usual
```

**Example** (cutoff 09:25; only three earlier sessions are shown to keep it short; the real average uses the previous 20):

| Session | 09:15 | 09:20 | 09:25 | Total to 09:25 |
|---|---|---|---|---|
| 19 Nov | 200 | 100 | 100 | 400 |
| 20 Nov | 300 | 200 | 100 | 600 |
| 23 Nov | 250 | 150 | 200 | 600 |
| **24 Nov (today)** | 700 | 500 | 300 | **1,500** |

The usual volume is (400 + 600 + 600) / 3 = 533.3, so **Day RVOL = 1,500 / 533.3 = 2.81×**.

**How it's used:** a strength point (≥ **1.5×**), the "RVOL" breakout confirmation, and the *Day RVOL* column.

---

## 14. Bar RVOL (volume-spike relative volume)

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
| Above the 2.5× alert threshold? | yes (§18 for the other conditions) |

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

| | Day RVOL | Bar RVOL |
|---|---|---|
| Question | Is the whole day busier than usual? | Is this minute a sudden burst? |
| Compares with | the same time of day on the previous 20 sessions | the previous 20 minutes |
| Candles | 5-min | 1-min |
| Used for | the score | ⚡ alerts |

---

## 15. How 1-minute candles are built from the live feed

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

## 16. Putting it together: votes, score, bias, filters, ranking

### 16.1 Six directional votes

| Vote | Bullish when | Bearish when | No vote when |
|---|---|---|---|
| VWAP | price > VWAP | price < VWAP | VWAP unknown |
| MACD | MACD line > Signal | MACD line < Signal | not enough data |
| DMI | +DI > −DI | −DI > +DI | not enough data |
| RS | RS ≥ +0.2 | RS ≤ −0.2 | between −0.2 and +0.2 |
| ORB | price > OR high | price < OR low | inside the range, or range not complete |
| EMA stack | price > EMA9 > EMA20 > EMA50 > EMA200 | price < EMA9 < EMA20 < EMA50 < EMA200 | EMAs out of order (mixed), or not enough candles |

With `EMA_STACK_VOTE = False` there are five votes and the maximum score is 7.

### 16.2 Bias and score

```
bull votes, bear votes = count of bullish / bearish votes (0–6 each)
strength = (ADX ≥ 20 ? 1 : 0) + (Day RVOL ≥ 1.5 ? 1 : 0)

Bullish  if bull votes ≥ 4 and bull votes > bear votes   →  score = bull votes + strength   (max 8)
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
| EMA stack | price > EMA9 > EMA20 > EMA50 > EMA200 → bull |
| ADX | 26 → +1 strength |
| Day RVOL | 1.2× → +0 strength |

That's 5 bull votes against 0 bear, so the stock is **Bullish**, with score 5 + 1 = **6/8**.
If the EMAs were mixed, it would still be Bullish (4 votes) with a score of **5/8**.

### 16.3 Filters (*Neutral & filtered* unless all pass)

| Filter | Rule |
|---|---|
| Daily history | loaded (turnover and ATR known) |
| Liquidity | average turnover ≥ ₹100 crore (§12) |
| Volatility | ATR % ≥ 1.5 (§6) |
| Series | in `INTRADAY_SERIES` (EQ). BE/BZ are trade-for-trade and SM/ST/SZ are SME, so they're tagged and filtered. |

### 16.4 Ranking

Within *Bullish* and within *Bearish*, stocks are sorted by **score**, then **ADX**, both highest
first. The first **5** get the **TOP** tag.

---

## 17. Breakout alerts

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
- **Filtered stocks only.** Only stocks that pass the filters (§16.3) can alert
  (`BREAKOUT_ALERTS_REQUIRE_FILTERS`).

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

---

## 18. Volume-spike (Bar RVOL) alerts

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
same five as for breakouts (§17), checked in that direction.

**Example:** at 10:31:05 the 10:30 candle completed with Bar RVOL **2.75×**, 22,000 shares at
about ₹1,254 (≈ ₹2.76 crore). It's after 09:35, finished seconds ago, there was no earlier alert in
10 minutes, and it's green, so the alert reads
**⚡ volume spike 2.8× on the 10:30 candle (+0.32 %)**.

---

## 19. Data windows and warm-up

| Data | Window loaded | Why |
|---|---|---|
| 5-min candles | 35 calendar days (about 23–25 sessions, ~1,800 candles) | lets EMA and Wilder's smoothing settle (EMA200 needs at least 200 candles); Day RVOL baseline of 20 previous sessions, with room for holidays |
| 1-min candles (REST mode) | 5 calendar days | 20-candle Bar RVOL window, covering weekends |
| 1-min candles (live mode) | built since the feed connected (last 120 kept) | Bar RVOL starts once 21 completed candles exist, ~21 min after connecting |
| Daily candles | 400 days | 52-week levels, 14-day ATR, 20-day turnover |

All of it is cached in `data/market.sqlite`, so later runs only fetch new candles.

---

## 20. Why values can differ from TradingView or your broker's chart

1. **Seeding.** EMA and Wilder's smoothing here start from the first value. Some platforms start
   from a simple average of the first *n* values. The difference fades over the ~1,800-candle warm-up,
   but very early values can differ.
2. **VWAP.** In REST mode, VWAP is approximated from 5-min typical prices. The exchange's VWAP (shown
   in live mode, and on most broker charts) uses every trade, so expect small differences.
3. **Forming candle.** In REST mode, if Dhan returns the still-forming 5-min candle, MACD, DMI and
   VWAP on the last candle move until it closes. Bar RVOL never uses a forming candle.
4. **RVOL definitions vary.** Platforms define "RVOL" differently: per candle, cumulative to time of
   day, or against the 50-day average volume. Check which one you're comparing with (§13 vs §14).
5. **Session gaps.** MACD and DMI run across sessions, so the first candle of a day includes the
   overnight gap in its TR and DM. Most charting platforms do the same.
6. **Data source.** Candle values come from Dhan. Another broker's feed can differ slightly.

---

## 21. All settings

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
| `MIN_AVG_TURNOVER_CR` | 100 | liquidity filter (₹ crore) |
| `OPENING_RANGE_MIN` | 15 | opening range length |
| `RS_MIN_PCT` | 0.2 | relative-strength vote threshold |
| `RVOL_LOOKBACK_SESSIONS` | 20 | Day RVOL baseline sessions |
| `RVOL_MIN` | 1.5 | Day RVOL strength point and confirmation |
| `MIN_DIRECTIONAL_VOTES` | 4 | votes needed (of 6) for bullish/bearish |
| `EMA_STACK_VOTE` | True | count the EMA stack as a sixth vote (score out of 8) |
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
| `DAILY_HISTORY_DAYS` | 400 | daily history loaded |
