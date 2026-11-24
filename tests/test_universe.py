from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

import config
from screener import universe as uv
from screener.dhan_client import DhanClient, DhanError, parse_company_info
from screener.engine import load_watchlist, load_watchlist_meta
from screener.store import MarketStore

MAIN = """SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE
INFY,Infosys Limited,EQ,08-FEB-1995,5,1,INE009A01021,5
TCS,Tata Consultancy Services Limited,EQ,25-AUG-2004,1,1,INE467B01029,1
WIPRO,Wipro Limited,EQ,08-NOV-1995,2,1,INE075A01022,2
HCLTECH,HCL Technologies Limited,EQ,06-JAN-2000,2,1,INE860A01027,2
SMALLIT,Small IT Limited,EQ,01-JAN-2010,10,1,INE000X01011,10
SMALLBANK,Small Bank Limited,BE,01-JAN-2011,10,1,INE000Y01011,10
HDFCBANK,HDFC Bank Limited,EQ,08-NOV-1995,1,1,INE040A01034,1
GONE,Suspended Limited,BZ,01-JAN-2001,10,1,INE000Z01011,10
"""
SME = """SYMBOL,NAME_OF_COMPANY,SERIES,DATE_OF_LISTING,PAID_UP_VALUE,ISIN_NUMBER,FACE_VALUE,
SMEPLAY,SME Play Limited,SM,10-Jan-24,10,INE111S01011,10,
SMEPLAY-RE1,SME Play Limited-RE,ST,16-Nov-26,10,INE111S20011,10,
"""
NSE_SECTORS = """Company Name,Industry,Symbol,Series,ISIN Code
Infosys Ltd.,Information Technology,INFY,EQ,INE009A01021
Tata Consultancy Services Ltd.,Information Technology,TCS,EQ,INE467B01029
Wipro Ltd.,Information Technology,WIPRO,EQ,INE075A01022
HCL Technologies Ltd.,Information Technology,HCLTECH,EQ,INE860A01027
HDFC Bank Ltd.,Financial Services,HDFCBANK,EQ,INE040A01034
"""


@pytest.fixture
def src(tmp_path):
    (tmp_path / "EQUITY_L.csv").write_text(MAIN)
    (tmp_path / "SME_EQUITY_L.csv").write_text(SME)
    (tmp_path / "sectors.csv").write_text(NSE_SECTORS)
    return tmp_path


IDS = {"INFY": "1594", "TCS": "11536", "WIPRO": "3787", "HCLTECH": "7229", "SMALLIT": "9001",
       "SMALLBANK": "9002", "HDFCBANK": "1333", "SMEPLAY": "9003"}          # GONE has no Dhan ID
INFO = {"1594": {"sector": "IT", "industry": "IT - Software"}, "11536": {"sector": "IT", "industry": "IT - Software"},
        "3787": {"sector": "IT", "industry": "IT - Software"}, "7229": {"sector": "IT", "industry": "IT - Software"},
        "9001": {"sector": "IT", "industry": "IT - Software"},           # -> Information Technology (4/4 agree)
        "1333": {"sector": "Finance", "industry": "Bank - Private"},
        "9002": {"sector": "Finance", "industry": "Bank - Private"},       # only 1 overlap stock: not enough
        "9003": {"sector": "Textiles", "industry": "Textile"}}             # no overlap, not an NSE name in the file


def test_read_nse_lists_merges_boards_and_drops_rights_entitlements(src):
    df = uv.read_nse_lists(src / "EQUITY_L.csv", src / "SME_EQUITY_L.csv")
    assert len(df) == 9 and "SMEPLAY-RE1" not in set(df["symbol"])
    assert df.set_index("symbol").loc["SMEPLAY", "board"] == "SME"
    assert df.set_index("symbol").loc["SMALLBANK", "series"] == "BE"
    assert df.set_index("symbol").loc["INFY", "isin"] == "INE009A01021"


def test_build_watchlist_assigns_sources_without_guessing(src):
    stocks = uv.read_nse_lists(src / "EQUITY_L.csv", src / "SME_EQUITY_L.csv")
    df = uv.build_watchlist(stocks, uv.read_nse_sectors(src / "sectors.csv"), IDS, INFO,
                            as_of=date(2026, 11, 24)).set_index("symbol")
    assert list(df.reset_index().columns) == uv.COLUMNS
    assert df.loc["INFY", "sector"] == "Information Technology" and df.loc["INFY", "sector_source"] == uv.SRC_NSE
    assert df.loc["SMALLIT", "sector"] == "Information Technology"
    assert df.loc["SMALLIT", "sector_source"] == uv.SRC_DHAN_INDUSTRY and df.loc["SMALLIT", "sector_confidence"] == 1.0
    assert df.loc["SMALLBANK", "sector_source"] == uv.SRC_UNMAPPED and pd.isna(df.loc["SMALLBANK", "sector"])
    assert df.loc["SMALLBANK", "dhan_industry"] == "Bank - Private"              # raw Dhan value kept
    assert df.loc["SMEPLAY", "sector_source"] == uv.SRC_UNMAPPED
    assert pd.isna(df.loc["GONE", "dhan_security_id"]) and df.loc["GONE", "sector_source"] == uv.SRC_UNMAPPED
    report = uv.coverage_report(df.reset_index())
    assert "9 NSE stocks" in report and "GONE" in report


def test_crosswalk_needs_agreement_and_overlap():
    df = pd.DataFrame({"dhan_industry": ["A"] * 3 + ["B"] * 4 + ["C"] * 2,
                       "nse_sector": ["X", "X", "X", "X", "X", "Y", "Y", "Z", "Z"]})
    xw = uv.build_crosswalk(df, "dhan_industry", min_share=0.75, min_count=3)
    assert xw == {"A": ("X", 1.0, 3)}            # B is split 50/50; C has only 2 overlap stocks


def test_nse_sector_matches_by_isin_then_symbol():
    stocks = pd.DataFrame({"symbol": ["NEWNAME", "OLDISIN"], "isin": ["INE1", "INE_CHANGED"]})
    nse = pd.DataFrame({"symbol": ["OLDNAME", "OLDISIN"], "isin": ["INE1", "INE_OLD"], "nse_sector": ["Power", "Realty"]})
    out = uv.attach_nse_sector(stocks, nse)
    assert list(out["nse_sector"]) == ["Power", "Realty"]


class FakeInfoClient:
    def __init__(self, fail_auth_after=None):
        self.calls, self.fail_auth_after = [], fail_auth_after

    def company_info(self, sid):
        self.calls.append(sid)
        if self.fail_auth_after is not None and len(self.calls) > self.fail_auth_after:
            raise DhanError("token expired", code="807")
        return {"SECTOR": "IT", "INDUSTRY": "IT - Software", "MARKET_CAP": "1000"}


def test_company_info_fetch_is_cached_and_resumable():
    store = MarketStore(":memory:")
    stocks = pd.DataFrame({"symbol": ["A", "B", "C", "D"], "dhan_security_id": ["1", "2", "3", None]})
    with pytest.raises(DhanError):
        uv.fetch_company_info(FakeInfoClient(fail_auth_after=2), store, stocks, progress=lambda m: None)
    assert set(store.company_info()) == {"1", "2"}                               # progress kept
    retry = FakeInfoClient()
    r = uv.fetch_company_info(retry, store, stocks, progress=lambda m: None)
    assert retry.calls == ["3"] and r["fetched"] == 1                             # only the missing one
    assert store.company_info()["3"]["sector"] == "IT" and store.company_info()["3"]["market_cap"] == 1000.0
    again = FakeInfoClient()
    uv.fetch_company_info(again, store, stocks, progress=lambda m: None)
    assert again.calls == []                                                      # all fresh


def test_company_info_request_and_parsing(monkeypatch):
    c = DhanClient("id", "token")
    seen = {}
    monkeypatch.setattr(c, "_post", lambda path, body, limiter: seen.update(path=path, body=body) or
                        {"data": {"CO": {"SECTOR": "Telecommunication", "INDUSTRY": "Telecom - Cellular & Fixed line services"}}})
    co = c.company_info("14366")
    assert seen["path"] == "/data/companyinfo"
    assert seen["body"] == {"securityId": "14366", "exchangeSegment": "NSE_EQ", "instrument": "EQUITY", "metrics": ["CO"]}
    assert co["SECTOR"] == "Telecommunication"
    assert parse_company_info({"status": "failure"}) == {} and parse_company_info(None) == {}


def test_watchlist_csv_loads_symbols_and_meta(tmp_path, src):
    stocks = uv.read_nse_lists(src / "EQUITY_L.csv", src / "SME_EQUITY_L.csv")
    df = uv.build_watchlist(stocks, uv.read_nse_sectors(src / "sectors.csv"), IDS, INFO)
    path = tmp_path / "watchlist.csv"
    df.to_csv(path, index=False)
    syms = load_watchlist(path)
    meta = load_watchlist_meta(path)
    assert len(syms) == 9 and syms[0] == "GONE"
    assert meta["INFY"] == {"sector": "Information Technology", "industry": "IT - Software",
                            "sector_source": uv.SRC_NSE, "company": "Infosys Limited", "board": "MAIN"}
    assert meta["SMALLBANK"]["sector"] is None
    assert load_watchlist_meta(config.NIFTY50_FILE) == {}                          # .txt lists have no meta


def test_feed_choice_for_big_watchlists():
    import app
    cfg = SimpleNamespace(REST_MAX_SYMBOLS=100)
    assert app.choose_feed("auto", "watchlist", 50, cfg) == "rest"
    assert app.choose_feed("auto", "watchlist", 3226, cfg) == "ws"
    assert app.choose_feed("auto", "nse", 50, cfg) == "ws"
    assert app.choose_feed("rest", "watchlist", 3226, cfg) == "rest"              # make_engine then refuses


def test_rows_carry_sector(tmp_path):
    from screener.demo_client import DemoClient
    from screener.engine import ScreenerEngine
    cfg = SimpleNamespace(**{k: getattr(config, k) for k in dir(config) if k.isupper()})
    symbols = load_watchlist(config.NIFTY50_FILE)[:4]
    meta = {symbols[0]: {"sector": "Information Technology", "industry": "IT - Software",
                         "sector_source": uv.SRC_NSE, "company": "X", "board": "MAIN"}}
    eng = ScreenerEngine(DemoClient(symbols, speed=1, start="11:00", benchmark=cfg.BENCHMARK), symbols, cfg, meta=meta)
    eng.bootstrap(); eng.refresh_candles(); eng.refresh_ltp()
    rows = {r["symbol"]: r for r in eng.rows}
    assert rows[symbols[0]]["sector"] == "Information Technology" and rows[symbols[1]]["sector"] is None


def test_exact_nse_sector_name_is_used_when_overlap_is_too_small():
    df = pd.DataFrame({
        "nse_sector": ["Forest Materials", None, None],
        "dhan_industry": ["Paper, Forest & Jute Products"] * 3,
        "dhan_sector": ["Forest Materials", "Forest Materials", "Paperish"],
    })
    out = uv.assign_sectors(df)
    assert list(out["sector_source"]) == [uv.SRC_NSE, uv.SRC_DHAN_SAME_NAME, uv.SRC_UNMAPPED]
    assert out["sector"].iloc[1] == "Forest Materials" and pd.isna(out["sector_confidence"].iloc[1])
