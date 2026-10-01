# 230X Problem Sets

Coursework for 230X (High-Frequency Finance).

## Setup

```
uv sync
cp .env.example .env      # then paste your own Databento key into .env
```

### Databento key

The equities notebook pulls raw Nasdaq ITCH order-book messages from Databento, which
needs a per-person API key. Get one at [databento.com](https://databento.com) (Portal →
Keys) and put it in `.env` at the repo root:

```
DATABENTO_API_KEY=db-your-key-here
```

`.env` is gitignored — never commit a key. An exported `DATABENTO_API_KEY` in your
shell also works and takes precedence.

`mbo.py` reads the key from there and downloads
`ps1/data/xnas-itch-20260902-fullday.mbo.csv.zst` on first use, printing the estimated
cost before transferring anything (about 0.6 GB billable; it was free on a standard
account in September 2026). The request runs from UTC midnight -- the only point at
which Databento snapshots the book -- to the 16:00 close, so the pre-market is
replayed and the book is complete when the regular session starts; only 09:30-16:00
is recorded. The download only happens once; the file and the derived cache under
`ps1/data/cache/` are both gitignored, so each collaborator downloads their own copy.
If you have an older `xnas-itch-20260902.mbo.csv.zst` (09:30 start) or `NVDA.npz` /
`GPRO.npz` caches from before 2026-09-07, delete them -- they are superseded.

## Structure

Each problem set has its own folder:

```
ps1/
  notebooks/
  data/
ps2/
  notebooks/
  data/
```

`algo2e/` is separate from the problem sets above — it's the team's working folder for
the third algorithmic trading case, not a graded assignment. See `algo2e/README.md`.

## Usage

```
uv run jupyter lab
```

## Notebooks

Each notebook is paired with a `.py` in jupytext percent format, and **the `.py` is the
source of truth** — it is what gets edited; the `.ipynb` is generated from it and is
what gets run. After editing a script, push the change into the notebook with:

```
uv run jupytext --sync ps1/notebooks/equities.py
```

`--sync` preserves the outputs of cells you have already run, and works in both
directions, so edits made in Jupyter flow back into the `.py`.

Three plain modules (not paired with notebooks) sit under `ps1/notebooks/`:

| module | what it does |
|---|---|
| `mbo.py` | replays the Databento MBO feed (Nasdaq ITCH) into an order book |
| `fx.py` | replays the EBS level-update feed into an order book, and computes triangular arbitrage |
| `hfstats.py` | homework parts (a)–(k) and the daily summary column, written once against whichever book it is handed |
| `plots.py` | the homework figures -- 5-minute liquidity, 30-minute return variance / autocorrelation, ACFs -- for either panel |

`mbo.Day` and `fx.Book` expose the same fields, so `hfstats` and `plots` compute the
same statistics and draw the same figures for both panels from one definition.
Figures and the per-panel summary tables are written to `ps1/figures/` when the
notebooks run; the report draft is in `ps1/report/`. Both reconstructions cache to
`ps1/data/cache/`; rebuild either with

```
uv run python ps1/notebooks/mbo.py     # download + full-day replay, needs the Databento key
uv run python ps1/notebooks/fx.py      # ~2s, reads the CSVs in ps1/data/
```
