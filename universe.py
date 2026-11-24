#!/usr/bin/env python3
"""Build and inspect watchlist.csv: every NSE-listed stock with its sector.

    python universe.py build              # NSE lists + NSE sectors + Dhan IDs + Dhan Company Info
    python universe.py build --no-dhan    # skip the Dhan Company Info step (no credentials needed)
    python universe.py report             # coverage summary of the current watchlist.csv

The Dhan step needs DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN (Data API plan). It makes one request per
stock (~3,200, about 14 minutes the first time), caches the answers in data/market.sqlite, and on
later runs only refreshes entries older than --info-max-age days.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

import config
from screener import universe as uv
from screener.dhan_client import DhanInstruments
from screener.store import MarketStore


def cmd_build(args) -> None:
    src_dir = config.DATA_DIR / "sources"
    print("1/4  Downloading NSE lists and NSE sector file …")
    files = uv.download_sources(src_dir, force=args.refresh)
    stocks = uv.read_nse_lists(files["nse_main"], files["nse_sme"])
    nse_sectors = uv.read_nse_sectors(files["nse_sectors"])
    print(f"     {len(stocks):,} NSE stocks; NSE sector for {len(nse_sectors):,} index constituents")

    print("2/4  Matching Dhan security IDs (public instrument list) …")
    instruments = DhanInstruments(cache_dir=config.DATA_DIR)
    ids = instruments.list_nse_equities(series=config.NSE_STOCK_SERIES)

    store = MarketStore(args.db)
    if args.no_dhan:
        print("3/4  Skipping Dhan Company Info (--no-dhan); using cached answers only")
    elif not (config.DHAN_CLIENT_ID and config.DHAN_ACCESS_TOKEN):
        print("3/4  Skipping Dhan Company Info: DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN not set in .env "
              "(re-run after setting them to fill sectors for the remaining stocks)")
    else:
        from screener.dhan_client import DhanClient, DhanError
        client = DhanClient(config.DHAN_CLIENT_ID, config.DHAN_ACCESS_TOKEN,
                            data_per_second=config.DATA_API_MAX_PER_SECOND, cache_dir=config.DATA_DIR)
        print("3/4  Dhan Company Info …")
        try:
            r = uv.fetch_company_info(client, store, uv.attach_dhan_ids(stocks, ids), args.info_max_age)
            print(f"     fetched {r['fetched']:,} of {r['requested']:,} ({r['errors']} errors)")
        except DhanError as exc:
            print(f"     stopped: Dhan rejected the credentials ({exc}). Progress so far is saved; "
                  "fix the token and re-run.")

    print("4/4  Assigning sectors and writing", args.out)
    df = uv.build_watchlist(stocks, nse_sectors, ids, store.company_info("NSE_EQ"),
                            min_share=args.min_agreement, min_count=args.min_overlap)
    df.to_csv(args.out, index=False)
    print("\n" + uv.coverage_report(df))


def cmd_report(args) -> None:
    path = Path(args.file)
    if not path.exists():
        sys.exit(f"{path} not found. Run: python universe.py build")
    df = pd.read_csv(path, dtype=str)
    print(uv.coverage_report(df))
    print("\nStocks per sector:")
    print(df["sector"].fillna("(unmapped)").value_counts().to_string())


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="download sources and write watchlist.csv")
    b.add_argument("--out", default=str(config.WATCHLIST_FILE))
    b.add_argument("--db", default=str(config.DB_PATH))
    b.add_argument("--no-dhan", action="store_true", help="don't call Dhan Company Info")
    b.add_argument("--refresh", action="store_true", help="re-download NSE files even if fresh")
    b.add_argument("--info-max-age", type=int, default=30, help="days before a Company Info answer is refreshed")
    b.add_argument("--min-agreement", type=float, default=0.75, help="crosswalk: share of overlap stocks that must agree")
    b.add_argument("--min-overlap", type=int, default=3, help="crosswalk: minimum overlap stocks per Dhan label")
    b.set_defaults(func=cmd_build)
    r = sub.add_parser("report", help="coverage of the current watchlist.csv")
    r.add_argument("--file", default=str(config.WATCHLIST_FILE))
    r.set_defaults(func=cmd_report)
    args = p.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    args.func(args)


if __name__ == "__main__":
    main()
