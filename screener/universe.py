"""Build watchlist.csv: every NSE-listed stock with its Dhan security ID and sector.

Sources (official, downloaded as files; no website scraping):
  1. NSE equity lists: main board EQUITY_L.csv + SME (Emerge) SME_EQUITY_L.csv -> the universe
  2. NSE Indices "Nifty Total Market" constituents (Industry column)          -> NSE sector, ~750 stocks
  3. Dhan instrument list (public)                                            -> Dhan security IDs
  4. Dhan Company Info API (Data API plan, needs your token)                  -> Dhan sector + industry

The canonical `sector` column uses NSE's 22-sector names. Where NSE has no sector for a stock, a
Dhan industry (or, failing that, a Dhan sector) is translated to an NSE sector only if the stocks
that have *both* labels agree strongly (default: at least 75 % of at least 3 stocks). Anything else
stays unmapped, except that a Dhan sector spelled exactly like one of NSE's sector names is taken
as that sector (labelled as such). Nothing is guessed. `sector_source` and `sector_confidence`
record which rule applied.
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Callable

import pandas as pd
import requests

log = logging.getLogger(__name__)

SOURCES = {
    "nse_main": ("https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv", "EQUITY_L.csv"),
    "nse_sme": ("https://nsearchives.nseindia.com/emerge/corporates/content/SME_EQUITY_L.csv", "SME_EQUITY_L.csv"),
    "nse_sectors": ("https://www.niftyindices.com/IndexConstituent/ind_niftytotalmarket_list.csv",
                    "ind_niftytotalmarket_list.csv"),
}
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
RIGHTS_ENTITLEMENT = re.compile(r"-RE\d*$")
COLUMNS = ["symbol", "company", "series", "board", "isin", "listing_date", "dhan_security_id",
           "sector", "sector_source", "sector_confidence", "nse_sector", "dhan_sector", "dhan_industry", "as_of"]
SRC_NSE = "NSE index file"
SRC_DHAN_INDUSTRY = "derived from Dhan industry"
SRC_DHAN_SECTOR = "derived from Dhan sector"
SRC_DHAN_SAME_NAME = "Dhan sector (same name as NSE)"
SRC_UNMAPPED = "unmapped"


# --------------------------------------------------------------------------- #
# Downloads
# --------------------------------------------------------------------------- #
def download_sources(dest: Path, max_age_days: float = 1, force: bool = False) -> dict[str, Path]:
    """Download the NSE files into `dest` (skipping fresh copies) and record them in sources.json."""
    dest.mkdir(parents=True, exist_ok=True)
    manifest_path = dest / "sources.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    out = {}
    for name, (url, fname) in SOURCES.items():
        path = dest / fname
        fresh = path.exists() and (datetime.now().timestamp() - path.stat().st_mtime) < max_age_days * 86400
        if not fresh or force:
            resp = requests.get(url, timeout=60, headers={"User-Agent": USER_AGENT, "Accept": "text/csv,*/*"})
            resp.raise_for_status()
            head = resp.content[:200].decode("utf-8", "replace").upper()
            if "SYMBOL" not in head:
                raise RuntimeError(f"{url} did not return the expected CSV (got: {head[:80]!r})")
            path.write_bytes(resp.content)
            manifest[name] = {"url": url, "file": fname, "downloaded_at": datetime.now().isoformat(timespec="seconds"),
                              "bytes": len(resp.content)}
        out[name] = path
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return out


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _norm_cols(df: pd.DataFrame) -> pd.DataFrame:
    df = df.loc[:, [c for c in df.columns if not str(c).startswith("Unnamed")]]
    return df.rename(columns=lambda c: re.sub(r"[\s_]+", " ", str(c)).strip().upper())


def read_nse_lists(main_path: Path, sme_path: Path) -> pd.DataFrame:
    """NSE main-board + SME equity lists -> symbol, company, series, isin, listing_date, board.
    Rights-entitlement lines (e.g. ABC-RE1) are dropped: they are not shares."""
    frames = []
    for path, board in ((main_path, "MAIN"), (sme_path, "SME")):
        df = _norm_cols(pd.read_csv(path, dtype=str))
        frames.append(pd.DataFrame({
            "symbol": df["SYMBOL"].str.strip().str.upper(),
            "company": df["NAME OF COMPANY"].str.strip(),
            "series": df["SERIES"].str.strip().str.upper(),
            "isin": df["ISIN NUMBER"].str.strip().str.upper(),
            "listing_date": df["DATE OF LISTING"].str.strip(),
            "board": board,
        }))
    out = pd.concat(frames, ignore_index=True)
    out = out[~out["symbol"].str.contains(RIGHTS_ENTITLEMENT)]
    return out.drop_duplicates("symbol").sort_values("symbol").reset_index(drop=True)


def read_nse_sectors(path: Path) -> pd.DataFrame:
    """NSE Indices constituents file -> symbol, isin, nse_sector (its 'Industry' column = NSE sector)."""
    df = _norm_cols(pd.read_csv(path, dtype=str))
    return pd.DataFrame({"symbol": df["SYMBOL"].str.strip().str.upper(),
                         "isin": df["ISIN CODE"].str.strip().str.upper(),
                         "nse_sector": df["INDUSTRY"].str.strip()})


def attach_nse_sector(stocks: pd.DataFrame, nse: pd.DataFrame) -> pd.DataFrame:
    by_isin = dict(zip(nse["isin"], nse["nse_sector"]))
    by_sym = dict(zip(nse["symbol"], nse["nse_sector"]))
    stocks = stocks.copy()
    stocks["nse_sector"] = [by_isin.get(i) or by_sym.get(s) for i, s in zip(stocks["isin"], stocks["symbol"])]
    return stocks


# --------------------------------------------------------------------------- #
# Dhan
# --------------------------------------------------------------------------- #
def attach_dhan_ids(stocks: pd.DataFrame, ids: dict[str, str]) -> pd.DataFrame:
    stocks = stocks.copy()
    stocks["dhan_security_id"] = stocks["symbol"].map(ids)
    return stocks


def fetch_company_info(client, store, stocks: pd.DataFrame, max_age_days: int = 30,
                       progress: Callable[[str], None] = print) -> dict:
    """Fill the company_info cache for every stock with a Dhan ID whose entry is missing or older
    than `max_age_days`. Resumable: a re-run only fetches what is still missing."""
    from .dhan_client import DhanError

    cached = store.company_info("NSE_EQ")
    todo = []
    for sym, sid in zip(stocks["symbol"], stocks["dhan_security_id"]):
        if not isinstance(sid, str) or not sid:
            continue
        row = cached.get(sid)
        age = (datetime.now() - datetime.fromisoformat(row["fetched_at"])).days if row else None
        if row is None or age >= max_age_days:
            todo.append((sym, sid))
    done = errors = 0
    if todo:
        progress(f"Fetching Dhan Company Info for {len(todo):,} stocks "
                 f"(~{len(todo) / 4 / 60:.0f} min at 4 requests/s; safe to stop and re-run)")
    for i, (sym, sid) in enumerate(todo, 1):
        try:
            store.upsert_company_info("NSE_EQ", sid, sym, client.company_info(sid))
            done += 1
        except DhanError as exc:
            errors += 1
            if exc.is_auth:
                raise
            log.warning("%s: company info failed: %s", sym, exc)
        if i % 200 == 0 or i == len(todo):
            progress(f"  {i:,}/{len(todo):,} done ({errors} errors)")
    return {"requested": len(todo), "fetched": done, "errors": errors}


def attach_company_info(stocks: pd.DataFrame, info: dict[str, dict]) -> pd.DataFrame:
    stocks = stocks.copy()
    stocks["dhan_sector"] = [(info.get(s) or {}).get("sector") if isinstance(s, str) else None
                             for s in stocks["dhan_security_id"]]
    stocks["dhan_industry"] = [(info.get(s) or {}).get("industry") if isinstance(s, str) else None
                               for s in stocks["dhan_security_id"]]
    return stocks


# --------------------------------------------------------------------------- #
# Sector assignment
# --------------------------------------------------------------------------- #
def build_crosswalk(stocks: pd.DataFrame, column: str, min_share: float = 0.75,
                    min_count: int = 3) -> dict[str, tuple[str, float, int]]:
    """Dhan label -> (NSE sector, share of overlap stocks agreeing, overlap size), learned only from
    stocks that have both labels; kept only when the agreement is strong enough."""
    counts: dict[str, Counter] = defaultdict(Counter)
    for dhan_label, nse_sector in zip(stocks[column], stocks["nse_sector"]):
        if isinstance(dhan_label, str) and dhan_label and isinstance(nse_sector, str) and nse_sector:
            counts[dhan_label][nse_sector] += 1
    out = {}
    for label, c in counts.items():
        sector, n = c.most_common(1)[0]
        total = sum(c.values())
        if total >= min_count and n / total >= min_share:
            out[label] = (sector, n / total, total)
    return out


def assign_sectors(stocks: pd.DataFrame, min_share: float = 0.75, min_count: int = 3) -> pd.DataFrame:
    by_industry = build_crosswalk(stocks, "dhan_industry", min_share, min_count)
    by_sector = build_crosswalk(stocks, "dhan_sector", min_share, min_count)
    # NSE's sector names, as they appear in the NSE file (used only for exact-name matches)
    nse_names = {s for s in stocks["nse_sector"] if isinstance(s, str) and s}
    sector, source, conf = [], [], []
    for nse_sector, ind, sec in zip(stocks["nse_sector"], stocks["dhan_industry"], stocks["dhan_sector"]):
        if isinstance(nse_sector, str) and nse_sector:
            sector.append(nse_sector); source.append(SRC_NSE); conf.append(1.0)
        elif isinstance(ind, str) and ind in by_industry:
            s, share, _ = by_industry[ind]
            sector.append(s); source.append(SRC_DHAN_INDUSTRY); conf.append(round(share, 3))
        elif isinstance(sec, str) and sec in by_sector:
            s, share, _ = by_sector[sec]
            sector.append(s); source.append(SRC_DHAN_SECTOR); conf.append(round(share, 3))
        elif isinstance(sec, str) and sec in nse_names:
            # too few overlap stocks to measure agreement, but Dhan uses NSE's exact sector name
            sector.append(sec); source.append(SRC_DHAN_SAME_NAME); conf.append(None)
        else:
            sector.append(None); source.append(SRC_UNMAPPED); conf.append(None)
    stocks = stocks.copy()
    stocks["sector"], stocks["sector_source"], stocks["sector_confidence"] = sector, source, conf
    return stocks


def build_watchlist(stocks: pd.DataFrame, nse_sectors: pd.DataFrame, ids: dict[str, str],
                    info: dict[str, dict], as_of: date | None = None, min_share: float = 0.75,
                    min_count: int = 3) -> pd.DataFrame:
    df = attach_nse_sector(stocks, nse_sectors)
    df = attach_dhan_ids(df, ids)
    df = attach_company_info(df, info)
    df = assign_sectors(df, min_share, min_count)
    df["as_of"] = (as_of or date.today()).isoformat()
    return df[COLUMNS]


def coverage_report(df: pd.DataFrame) -> str:
    n = len(df)
    lines = [f"{n:,} NSE stocks ({(df['board'] == 'MAIN').sum():,} main board, {(df['board'] == 'SME').sum():,} SME)",
             f"  with a Dhan security ID: {df['dhan_security_id'].notna().sum():,}"
             + (f"  (missing: {', '.join(df.loc[df['dhan_security_id'].isna(), 'symbol'].head(15))}"
                f"{'…' if df['dhan_security_id'].isna().sum() > 15 else ''})" if df["dhan_security_id"].isna().any() else ""),
             f"  with Dhan Company Info: {df['dhan_sector'].notna().sum():,}",
             "  sector by source:"]
    for src, cnt in df["sector_source"].value_counts().items():
        lines.append(f"    {src:<28} {cnt:>5,}  ({cnt / n:.0%})")
    lines.append(f"  distinct sectors: {df['sector'].nunique()}")
    return "\n".join(lines)
