# 230X Problem Sets

Coursework for 230X (High-Frequency Finance).

## Setup

```
uv sync
```

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

`ps1/notebooks/mbo.py` is a plain module (not paired): it replays the Databento MBO
feed into an order book and caches the result under `ps1/data/cache/`. The first build
takes ~15s; after that it loads from cache. Rebuild with `uv run python ps1/notebooks/mbo.py`.
