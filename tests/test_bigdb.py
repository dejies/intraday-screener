"""A multi-GB database (a full backfill: tens of millions of candles) must not make `manage.py check`
(run by scripts/start.*) or `history.py status` scan every candle: both read the small coverage table."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from types import SimpleNamespace

import manage
from screener.store import MarketStore


def db_with_coverage_only(tmp_path):
    path = tmp_path / "big.sqlite"
    st = MarketStore(str(path))
    st.upsert_instruments({"AAA": "1", "BBB": "2"}, segment="NSE_EQ")
    st.upsert_instruments({"NIFTY 50": "13"}, segment="IDX_I")
    for sid in ("1", "2"):
        st.extend_coverage("NSE_EQ", sid, "D", datetime(2024, 1, 1), datetime(2026, 11, 26))
        st.extend_coverage("NSE_EQ", sid, "5", datetime(2025, 11, 26, 9), datetime(2026, 11, 26, 15, 30))
    st.extend_coverage("IDX_I", "13", "5", datetime(2025, 11, 26, 9), datetime(2026, 11, 26, 15, 30))
    st.close()
    return path


def test_check_reads_coverage_not_candles(tmp_path, monkeypatch):
    path = db_with_coverage_only(tmp_path)          # no candles at all: anything reported came from coverage
    r = manage.Report()
    manage.check_db(r, path, quick=True)
    got = {what: detail for _, what, detail in r.items}
    assert got["candles NSE_EQ D"] == "2 series, 2024-01-01 to 2026-11-26"
    assert got["candles NSE_EQ 5-min"] == "2 series, 2025-11-26 to 2026-11-26"
    assert got["candles IDX_I 5-min"].startswith("1 series") and "index 5-min candles" not in got


def test_status_overview_from_coverage_and_exact_rows_for_one_symbol(tmp_path, capsys):
    import history
    path = db_with_coverage_only(tmp_path)
    con = sqlite3.connect(path)
    con.executemany("INSERT INTO candles VALUES ('NSE_EQ','1','D',?,1,1,1,1,10)", [(f"2026-11-{d:02d}",) for d in (23, 24, 25)])
    con.commit()
    con.close()
    history.cmd_status(SimpleNamespace(db=str(path), symbol=None, all_rows=False))
    out = capsys.readouterr().out
    assert "5 series" in out and "exact counts: --symbol X" in out and "AAA" in out and "BBB" in out
    history.cmd_status(SimpleNamespace(db=str(path), symbol="aaa", all_rows=False))
    out = capsys.readouterr().out
    assert "3 candles" in out and "2026-11-23" in out and "BBB" not in out
