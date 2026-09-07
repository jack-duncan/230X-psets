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

`mbo.py` reads the key from there and downloads `ps1/data/xnas-itch-20260902.mbo.csv.zst`
on first use, printing the estimated cost before transferring anything. The download is
billed by volume and only happens once; the file and the derived cache under
`ps1/data/cache/` are both gitignored, so each collaborator downloads their own copy.

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
| `hfstats.py` | homework parts (a)–(k), written once against whichever book it is handed |

`mbo.Day` and `fx.Book` expose the same fields, so `hfstats` computes the same
statistics for both panels from one definition. Both reconstructions cache to
`ps1/data/cache/`; rebuild either with

```
uv run python ps1/notebooks/mbo.py     # ~15s, needs the Databento key
uv run python ps1/notebooks/fx.py      # ~2s, reads the CSVs in ps1/data/
```
