"""Read-only DhanHQ v2 REST client: instrument lookup, candles and live prices.

This client never places orders. Docs: https://dhanhq.co/docs/v2/
"""
from __future__ import annotations

import io
import logging
import threading
import time as _time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

log = logging.getLogger(__name__)

BASE_URL = "https://api.dhan.co/v2"
SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
IST = "Asia/Kolkata"

AUTH_ERROR_CODES = {"DH-901", "807", "808", "809", "810"}
RATE_LIMIT_CODES = {"DH-904", "805"}
# NSE stock series: EQ normal | BE/BZ trade-for-trade (delivery only) | SM/ST/SZ SME platform
STOCK_SERIES = ("EQ", "BE", "BZ", "SM", "ST", "SZ")


class DhanError(RuntimeError):
    def __init__(self, message: str, code: str | None = None, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status

    @property
    def is_auth(self) -> bool:
        return self.status == 401 or (self.code or "") in AUTH_ERROR_CODES

    @property
    def is_rate_limit(self) -> bool:
        return self.status == 429 or (self.code or "") in RATE_LIMIT_CODES


class RateLimiter:
    """Spaces calls at least 1/rate seconds apart (thread-safe)."""

    def __init__(self, per_second: float):
        self.min_interval = 1.0 / per_second
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = _time.monotonic()
            if now < self._next:
                _time.sleep(self._next - now)
                now = self._next
            self._next = now + self.min_interval


def candles_to_df(payload: dict) -> pd.DataFrame:
    """Dhan returns column arrays + UNIX epoch seconds. Convert to a DataFrame
    indexed by naive IST timestamps."""
    cols = ["open", "high", "low", "close", "volume"]
    if not payload or not payload.get("timestamp"):
        return pd.DataFrame(columns=cols, index=pd.DatetimeIndex([], name="ts"))
    idx = (
        pd.to_datetime(pd.Series(payload["timestamp"], dtype="int64"), unit="s", utc=True)
        .dt.tz_convert(IST)
        .dt.tz_localize(None)
    )
    df = pd.DataFrame({c: pd.to_numeric(pd.Series(payload.get(c, [])), errors="coerce") for c in cols})
    df.index = pd.DatetimeIndex(idx, name="ts")
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df.dropna(subset=["open", "high", "low", "close"])


def parse_company_info(payload) -> dict:
    """Find the CO block in a Company Info response ({"data": {"CO": {...}}}, defensively)."""
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data", payload)
    if isinstance(data, list) and data:
        data = data[0]
    co = data.get("CO") if isinstance(data, dict) else None
    if isinstance(co, list) and co:
        co = co[0]
    return co if isinstance(co, dict) else {}


def _pick(columns, candidates: list[str]) -> str | None:
    for c in candidates:
        if c in columns:
            return c
    return None


class DhanInstruments:
    """Dhan's public instrument list (no credentials needed): NSE stock lookup and listing."""

    def __init__(self, cache_dir: Path | None = None):
        self.cache_dir = cache_dir
        self.resolve_notes: dict[str, str] = {}  # why a symbol was skipped (see resolve_security_ids)
        self.series: dict[str, str] = {}         # symbol -> NSE series (EQ, BE, SM, ...)

    # -------------------------------------------------------------- instruments
    def _scrip_master(self) -> pd.DataFrame:
        cache = self.cache_dir / "scrip_master.csv" if self.cache_dir else None
        if cache and cache.exists() and datetime.fromtimestamp(cache.stat().st_mtime).date() == date.today():
            return pd.read_csv(cache, low_memory=False)
        resp = requests.get(SCRIP_MASTER_URL, timeout=60)
        resp.raise_for_status()
        if cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(resp.content)
        return pd.read_csv(io.BytesIO(resp.content), low_memory=False)

    def _nse_stocks(self, series_allowed):
        """Dhan instrument list with helper columns, plus the mask of NSE equity shares.

        An NSE stock = exchange NSE, segment E, instrument EQUITY, exchange instrument
        type ES (equity share - excludes ETFs, bonds, G-secs and NSE test symbols) and
        a stock series (EQ, BE, BZ, SM, ST, SZ by default)."""
        df = self._scrip_master()
        cols = df.columns
        exch = _pick(cols, ["SEM_EXM_EXCH_ID", "EXCH_ID"])
        seg = _pick(cols, ["SEM_SEGMENT", "SEGMENT"])
        inst = _pick(cols, ["SEM_INSTRUMENT_NAME", "INSTRUMENT"])
        itype = _pick(cols, ["SEM_EXCH_INSTRUMENT_TYPE", "INSTRUMENT_TYPE"])
        sid = _pick(cols, ["SEM_SMST_SECURITY_ID", "SECURITY_ID", "SEM_SECURITY_ID"])
        sym = _pick(cols, ["SEM_TRADING_SYMBOL", "UNDERLYING_SYMBOL", "SYMBOL_NAME"])
        series = _pick(cols, ["SEM_SERIES", "SERIES"])
        if not (exch and sid and sym):
            raise DhanError(f"Unexpected scrip-master columns: {list(cols)[:20]}")
        up = lambda c: df[c].fillna("").astype(str).str.upper().str.strip()   # noqa: E731
        df = df.assign(_sym=up(sym), _exch=up(exch), _series=up(series) if series else "EQ",
                       _itype=up(itype) if itype else "ES", _sid=df[sid])
        cash = df["_exch"] == "NSE"
        if seg:
            cash &= up(seg) == "E"
        if inst:
            cash &= up(inst) == "EQUITY"
        stock = cash & (df["_itype"] == "ES") & df["_series"].isin([x.upper() for x in series_allowed])
        return df, cash, stock

    @staticmethod
    def _pick_listing(rows: pd.DataFrame) -> pd.Series:
        """Prefer the EQ series when a symbol has several listings."""
        return rows.assign(_rank=(rows["_series"] != "EQ").astype(int)).sort_values("_rank").iloc[0]

    def list_nse_equities(self, series: tuple[str, ...] = STOCK_SERIES) -> dict[str, str]:
        """Every NSE equity share in Dhan's instrument list: {symbol: security_id}.
        The series of each stock is kept in `self.series`."""
        df, _cash, stock = self._nse_stocks(series)
        st = df[stock].assign(_rank=lambda d: (d["_series"] != "EQ").astype(int))
        first = st.sort_values(["_sym", "_rank"]).drop_duplicates("_sym")
        self.series = dict(zip(first["_sym"], first["_series"]))
        return {s: str(int(float(i))) for s, i in zip(first["_sym"], first["_sid"])}

    def resolve_security_ids(self, symbols: list[str], series: tuple[str, ...] = STOCK_SERIES) -> dict[str, str]:
        """Map NSE trading symbols (e.g. RELIANCE) to Dhan security IDs.

        Only NSE equity shares are accepted (see `_nse_stocks`). BSE-only listings,
        ETFs, indices, bonds and F&O contracts are skipped, with the reason in
        `resolve_notes`. Each stock's series (EQ, BE, SM...) goes to `self.series`."""
        df, cash, stock = self._nse_stocks(series)
        groups = df.groupby("_sym").indices
        out: dict[str, str] = {}
        self.resolve_notes = {}
        for s in symbols:
            key = s.upper()
            idx = groups.get(key)
            if idx is None:
                self.resolve_notes[s] = "not in Dhan's instrument list"
                continue
            rows = df.iloc[idx]
            ok = rows[stock.iloc[idx].to_numpy()]
            if ok.empty:
                nse_rows = rows[cash.iloc[idx].to_numpy()]
                if not (rows["_exch"] == "NSE").any():
                    self.resolve_notes[s] = f"listed on {'/'.join(sorted(set(rows['_exch'])))} only"
                elif nse_rows.empty:
                    self.resolve_notes[s] = "no NSE cash-market listing"
                elif (nse_rows["_itype"] == "ETF").any():
                    self.resolve_notes[s] = "an ETF, not a stock"
                else:
                    r = nse_rows.iloc[0]
                    self.resolve_notes[s] = f"NSE {r['_itype'] or 'instrument'} in series {r['_series'] or '-'}, not an equity share"
                continue
            best = self._pick_listing(ok)
            out[s] = str(int(float(best["_sid"])))
            self.series[s] = best["_series"]
        return out


class DhanClient(DhanInstruments):
    label = "Dhan (live)"

    def __init__(self, client_id: str, access_token: str, data_per_second: float = 4,
                 cache_dir: Path | None = None, timeout: float = 15):
        if not client_id or not access_token:
            raise ValueError(
                "DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN must be set (see .env.example)."
            )
        super().__init__(cache_dir)
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "access-token": access_token,
            "client-id": client_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        self._data_limiter = RateLimiter(data_per_second)
        self._quote_limiter = RateLimiter(1.0)   # documented: 1 request / second
        self._interval_as_int = False            # docs show "5" but call it an integer enum
        self._interval_confirmed = False         # set once a call succeeds with the current format

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def now() -> datetime:
        return pd.Timestamp.now(tz=IST).tz_localize(None).to_pydatetime()

    def _post(self, path: str, body: dict, limiter: RateLimiter, retries: int = 2) -> dict:
        for attempt in range(retries + 1):
            limiter.wait()
            try:
                resp = self.session.post(BASE_URL + path, json=body, timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt < retries:
                    _time.sleep(1 + attempt)
                    continue
                raise DhanError(f"Network error calling {path}: {exc}") from exc
            if resp.ok:
                data = resp.json()
                if isinstance(data, dict) and data.get("status") == "failure":
                    raise DhanError(self._error_text(data), self._error_code(data), resp.status_code)
                return data
            try:
                err = resp.json()
            except ValueError:
                err = {"errorMessage": resp.text[:300]}
            error = DhanError(f"{path}: HTTP {resp.status_code} {self._error_text(err)}",
                              self._error_code(err), resp.status_code)
            if error.is_rate_limit and attempt < retries:
                _time.sleep(2 * (attempt + 1))
                continue
            raise error
        raise DhanError(f"{path}: retries exhausted")

    @staticmethod
    def _error_code(err) -> str | None:
        if isinstance(err, dict):
            for k in ("errorCode", "error_code", "code"):
                if err.get(k) is not None:
                    return str(err[k])
            data = err.get("data")
            if isinstance(data, dict) and len(data) == 1:   # e.g. {"data": {"806": "Data APIs not Subscribed"}}
                return str(next(iter(data)))
        return None

    @staticmethod
    def _error_text(err) -> str:
        if isinstance(err, dict):
            for k in ("errorMessage", "message", "remarks", "data"):
                if err.get(k):
                    return str(err[k])[:300]
        return str(err)[:300]

    # ------------------------------------------------------------------ candles
    def daily_candles(self, security_id: str, from_date: date, to_date: date,
                      segment: str = "NSE_EQ", instrument: str = "EQUITY") -> pd.DataFrame:
        body = {
            "securityId": str(security_id),
            "exchangeSegment": segment,
            "instrument": instrument,
            "expiryCode": 0,
            "oi": False,
            "fromDate": from_date.strftime("%Y-%m-%d"),
            "toDate": (to_date + timedelta(days=1)).strftime("%Y-%m-%d"),  # toDate is exclusive
        }
        df = candles_to_df(self._post("/charts/historical", body, self._data_limiter))
        df.index = df.index.normalize()
        return df

    def intraday_candles(self, security_id: str, from_dt: datetime, to_dt: datetime,
                         interval: int = 5, segment: str = "NSE_EQ",
                         instrument: str = "EQUITY") -> pd.DataFrame:
        def body():
            return {
                "securityId": str(security_id),
                "exchangeSegment": segment,
                "instrument": instrument,
                "interval": int(interval) if self._interval_as_int else str(interval),
                "oi": False,
                "fromDate": from_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "toDate": to_dt.strftime("%Y-%m-%d %H:%M:%S"),
            }
        try:
            payload = self._post("/charts/intraday", body(), self._data_limiter)
        except DhanError as exc:
            if exc.is_auth or exc.is_rate_limit or self._interval_confirmed:
                raise
            # The docs disagree on whether interval is a string or an integer:
            # until one format has worked, try the other once.
            self._interval_as_int = not self._interval_as_int
            try:
                payload = self._post("/charts/intraday", body(), self._data_limiter)
            except DhanError:
                self._interval_as_int = not self._interval_as_int
                raise exc
        self._interval_confirmed = True
        return candles_to_df(payload)

    # ------------------------------------------------------------- company info
    def company_info(self, security_id: str, segment: str = "NSE_EQ") -> dict:
        """Company profile (Data API plan): POST /v2/data/companyinfo with metrics ["CO"].
        Returns the CO block, e.g. {"SECTOR": "Telecommunication", "INDUSTRY": "...", "MARKET_CAP": ...}."""
        body = {"securityId": str(security_id), "exchangeSegment": segment, "instrument": "EQUITY",
                "metrics": ["CO"]}
        payload = self._post("/data/companyinfo", body, self._data_limiter)
        return parse_company_info(payload)

    # --------------------------------------------------------------- live price
    def ltp(self, instruments: dict[str, list[str]]) -> dict[tuple[str, str], float]:
        """instruments: {"NSE_EQ": ["2885", ...], "IDX_I": ["13"]} -> {(segment, id): price}"""
        body = {seg: [int(i) for i in ids] for seg, ids in instruments.items() if ids}
        data = self._post("/marketfeed/ltp", body, self._quote_limiter).get("data") or {}
        out: dict[tuple[str, str], float] = {}
        for seg, items in data.items():
            for sec_id, quote in (items or {}).items():
                price = quote.get("last_price") if isinstance(quote, dict) else None
                if price is not None:
                    out[(seg, str(sec_id))] = float(price)
        return out
