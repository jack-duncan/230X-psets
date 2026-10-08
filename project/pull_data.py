"""Pull September 2026 MBP-1 data from Databento for the final project.

Usage (from the repo root):
    uv run python project/pull_data.py check    # list datasets/schemas + cost of the full month (free)
    uv run python project/pull_data.py test     # pull the GPRO test day and print a quick summary
    uv run python project/pull_data.py pull     # pull the whole month (asks before spending)

Notes:
- Window is 09:30-16:00 ET (13:30-20:00 UTC in September, EDT). The strategy trades
  09:40-15:50, but the lookbacks need the earlier data to warm up, so filter after loading.
- Use ts_event (exchange time) as the clock for the 0 / 100 ms / 1 s delays, not ts_recv.
- XNAS.ITCH is Nasdaq-only, so the "NBBO" here is Nasdaq's top of book.
- Each day is saved as raw .dbn.zst, then converted to Parquet one day at a time.
"""

import argparse
import os
import sys
from pathlib import Path

import databento as db
import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parent
REPO_ROOT = PROJECT_DIR.parent
RAW_DIR = PROJECT_DIR / "data" / "raw"
PARQUET_DIR = PROJECT_DIR / "data" / "parquet"

SCHEMA = "mbp-1"
MONTH_START, MONTH_END = "2026-09-01", "2026-09-30"
HOLIDAYS = {"2026-09-07"}  # Labor Day
TEST_DAY = "2026-09-14"

# (dataset, symbol, stype_in)
JOBS = [
    ("XNAS.ITCH", "AAPL", "raw_symbol"),
    ("XNAS.ITCH", "GPRO", "raw_symbol"),
    ("GLBX.MDP3", "6E.v.0", "continuous"),
]


def client() -> db.Historical:
    """Databento client, reading DATABENTO_API_KEY from the environment or the repo's .env."""
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
    key = os.environ.get("DATABENTO_API_KEY", "").strip()
    if not key.startswith("db-") or "xxxx" in key:
        sys.exit("Set DATABENTO_API_KEY in .env (copy .env.example) or export it in your shell.")
    return db.Historical(key)


def req(dataset: str, symbol: str, stype: str, day: str) -> dict:
    # 09:30-16:00 ET in September (EDT) = 13:30-20:00 UTC
    return dict(dataset=dataset, symbols=[symbol], schema=SCHEMA, stype_in=stype,
                start=f"{day}T13:30:00", end=f"{day}T20:00:00")


def trading_days() -> list[str]:
    days = pd.bdate_range(MONTH_START, MONTH_END).strftime("%Y-%m-%d")
    return [d for d in days if d not in HOLIDAYS]


def paths(dataset: str, symbol: str, day: str) -> tuple[Path, Path]:
    stem = f"{dataset}_{symbol}_{day}"
    return RAW_DIR / f"{stem}.dbn.zst", PARQUET_DIR / f"{stem}.parquet"


def month_cost(c: db.Historical, jobs=JOBS) -> float:
    """Sum get_cost over every (job, day) still missing on disk. Metadata calls are free."""
    total = 0.0
    for ds, sym, st in jobs:
        job_cost = sum(c.metadata.get_cost(**req(ds, sym, st, d))
                       for d in trading_days() if not paths(ds, sym, d)[0].exists())
        print(f"  {ds:10s} {sym:8s} ${job_cost:,.2f}")
        total += job_cost
    print(f"  {'total':19s} ${total:,.2f}")
    return total


def pull_day(c: db.Historical, ds: str, sym: str, st: str, day: str) -> Path:
    """Download one day to .dbn.zst (if missing) and convert it to Parquet (if missing)."""
    raw, pq = paths(ds, sym, day)
    if not raw.exists():
        print(f"  downloading {raw.name}")
        c.timeseries.get_range(**req(ds, sym, st, day), path=raw)
    if not pq.exists():
        df = db.DBNStore.from_file(raw).to_df()  # index = ts_recv; prices in dollars
        df.to_parquet(pq)
        print(f"  wrote {pq.name} ({len(df):,} rows)")
    return pq


def cmd_check(c: db.Historical) -> None:
    datasets = c.metadata.list_datasets()
    for ds in sorted({j[0] for j in JOBS}):
        ok = ds in datasets
        schemas = c.metadata.list_schemas(ds) if ok else []
        print(f"{ds}: {'found' if ok else 'NOT FOUND'}; {SCHEMA} {'available' if SCHEMA in schemas else 'MISSING'}")
    print(f"\nCost of remaining downloads, {len(trading_days())} trading days:")
    month_cost(c)


def cmd_test(c: db.Historical) -> None:
    ds, sym, st = next(j for j in JOBS if j[1] == "GPRO")
    print(f"Test day {TEST_DAY}, {sym}: ${c.metadata.get_cost(**req(ds, sym, st, TEST_DAY)):,.2f}")
    df = pd.read_parquet(pull_day(c, ds, sym, st, TEST_DAY))
    print(df.head())
    print(f"\n{len(df):,} updates; actions:\n{df['action'].value_counts().to_string()}")


def cmd_pull(c: db.Historical, yes: bool) -> None:
    print("Cost of remaining downloads:")
    total = month_cost(c)
    if total > 0 and not yes and input("Proceed? [y/N] ").strip().lower() != "y":
        sys.exit("Aborted.")
    for day in trading_days():
        print(day)
        for ds, sym, st in JOBS:
            pull_day(c, ds, sym, st, day)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["check", "test", "pull"])
    p.add_argument("-y", "--yes", action="store_true", help="skip the cost confirmation for 'pull'")
    args = p.parse_args()

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    PARQUET_DIR.mkdir(parents=True, exist_ok=True)
    c = client()
    if args.command == "check":
        cmd_check(c)
    elif args.command == "test":
        cmd_test(c)
    else:
        cmd_pull(c, args.yes)


if __name__ == "__main__":
    main()
