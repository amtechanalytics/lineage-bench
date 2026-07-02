# OmniMart Column-Lineage Hard-Transform Benchmark

A public, transform-type-stratified benchmark for evaluating **column-level
data-lineage extraction** on the hard cases that appear in real medallion
warehouses without referential integrity. Three extraction methods are compared
on identical SQL text and scored against a hand-authored, execution-validated
gold edge set:

- **SQLGlot** static parser (`sqlglot.lineage`)
- **sqllineage** static parser
- **LLM arm** (reads SQL text, emits column edges)

The benchmark isolates two orthogonal difficulty axes:

1. **Structural complexity** — row-to-column pivots (static and dynamic),
   window / CASE derived calculations, lateral flatten of semi-structured
   data, and foreign-key-free cross-source merges.
2. **Data-product width + calculation density** — wide rename-heavy views with
   repeated column families, and calculation-heavy views with many-to-one
   derived expressions. This axis reflects the enterprise "data product" layer
   pattern.

## Reproducibility posture

The **benchmark artifact is the SQL text plus `gold/gold_edges.json`.** All
three extraction arms operate on **static SQL and require no database.**
Snowflake is used once, by the benchmark author, only to execute the models and
certify that the gold edges are real (see `warehouse/validate/`). Anyone can
reproduce the full static evaluation with `pip install -r requirements.txt`; the
LLM arm additionally needs an API key.

## Repository layout

```
warehouse/            Snowflake SQL defining the benchmark warehouse
  00_setup.sql        database + schema context
  bronze/bronze.sql   raw landing tables + seed data (no PK/FK)
  silver/silver.sql   conformed models (no-RI merge, flatten, join, aggregate)
  gold/gold.sql       marts (static/dynamic pivot, window calc, nested, CTE)
  gold/dp_wide.sql    DP archetype A: wide rename views (repeated families)
  gold/dp_calc.sql    DP archetype B: calculation-heavy views
  validate/validate.sql            SELECTs to inspect each hard model (manual check)
  validate/certify_gold_proc.sql   stored proc: 10 baked-in PASS/FAIL checks + stage
  validate/run_certification.sql   driver: verdict table, JSON blob, file download
gold/
  gold_edges.json     ground-truth column->column edges (DIRECT/INDIRECT, tagged by transform type)
arms/                 extraction runners (added in Phase 1 / 3)
harness/              scoring + statistics (added in Phase 2)
figures/              figure + table generators (added in Phase 4)
results/              run outputs (raw dumps git-ignored)
requirements.txt      pinned Python deps for arms + scoring
```

## Warehouse model taxonomy

Structural axis:

| Model | Transform type | Difficulty |
|-------|----------------|-----------|
| stg_products | T1 projection/rename | easy |
| stg_order_items_enriched | T2 join | easy-med |
| stg_sales_base | T3 aggregate | med |
| rpt_exec_summary | T4 CTE chain | med |
| fct_sales_wide_static | T5a static PIVOT | hard |
| fct_category_pivot_dyn | T5b dynamic PIVOT | hardest |
| dim_customer_metrics | T6 derived calc (window/CASE) | hard |
| stg_orders_unified | T7 no-RI merge | hard |
| stg_events_flat | TFL lateral flatten | hard |
| mart_seller_scorecard | T5b + T6 nested | hardest |

Data-product axis:

| Model | Transform type | Difficulty |
|-------|----------------|-----------|
| dp_item_catalog | DP_A wide rename (~23 cols) | med (width) |
| dp_party_360 | DP_A wide rename (~40 cols, 4 families) | hard (width + cross-wire bait) |
| dp_settlement_summary | DP_B calc-heavy (rebate/settlement) | hard (many-to-one) |
| dp_order_economics | DP_B calc-heavy (order P&L + window) | hard (many-to-one + INDIRECT) |

**Gold set: 206 column-to-column edges** across 14 transform models, 11
transform types, 180 DIRECT / 26 INDIRECT.

## Gold edge schema

`gold/gold_edges.json` contains a `_meta` block (taxonomy, edge semantics) and
an `edges` array. Each edge:

```json
{
  "target_model":   "dp_settlement_summary",
  "target_column":  "rebate_amount",
  "source_table":   "ph_settlement_base",
  "source_column":  "qual_purch_amt",
  "type":           "DIRECT",
  "transform_type": "DP_B"
}
```

- **DIRECT** = output value is derived from the source value.
- **INDIRECT** = source influences the output (filter / group / partition /
  order / join key) but is not the value source.
- Dynamic-pivot output columns are represented with the sentinel
  `target_column = "__PIVOTED__"` because their names are determined at run time
  by the data, not the SQL text; the scorer handles this class specially.

## How to reproduce

### Step 1 — (author only) build the warehouse in Snowflake

Run in a Snowflake worksheet, in order:

```
warehouse/00_setup.sql
warehouse/bronze/bronze.sql
warehouse/silver/silver.sql
warehouse/gold/gold.sql
warehouse/gold/dp_wide.sql
warehouse/gold/dp_calc.sql
```

### Step 1b — certify the gold

The gold edges are certified by executing the models and confirming each hard
transform produced the expected shape. Two ways:

**Automated (recommended).** Run `warehouse/validate/certify_gold_proc.sql`
once to create the `certify_gold()` procedure and `CERT_STAGE`. Then run the
blocks in `warehouse/validate/run_certification.sql`:

- Block A — verdict table, one row per check (PASS/FAIL float to top)
- Block B — one-row overall PASS/FAIL summary
- Block C — the single JSON blob (copy the one cell for the record)
- Block D — writes `certification.json` to `@CERT_STAGE`
- Block E — `GET` to download the file (SnowSQL only, not the browser console)

The procedure runs 10 baked-in checks: no-RI merge rowcount and seller
resolution, DP-A family distinctness (cross-wiring guard), DP-B derived math
(`rebate_amount`, `net_revenue` recomputed and compared), static + dynamic
pivot columns, flatten columns, and wide-view width. `overall = PASS` means the
gold is certified.

**Manual fallback.** Run `warehouse/validate/validate.sql` query by query and
confirm each hard model matches the expected shape in its comment. This needs
no stored procedure.

This whole step is **not required to run the evaluation** — it only validates
the ground truth.

### Step 2 — set up the Python environment

```
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### Step 3 — run the static arms (no database, no API, free)

```
python arms/run_sqlglot.py
python arms/run_sqllineage.py
```

Each writes predicted edges to `results/<arm>_edges.json`.

### Step 4 — score

```
python harness/score.py
```

Produces per-transform-type precision / recall / F1, DIRECT/INDIRECT
breakdowns, Wilson confidence intervals, and paired significance tests in
`results/`.

### Step 5 — run the LLM arm (requires API key)

```
export ANTHROPIC_API_KEY=...        # set your key
python arms/run_llm.py              # canary-gated, cost-capped
python harness/score.py             # re-score all three arms
```

### Step 6 — figures + tables

```
python figures/make_figures.py
python figures/make_tables.py
```

## Git workflow

Initial setup (empty remote already created):

```
git clone git@github.com:<you>/lineage-bench.git
cd lineage-bench
# copy repo contents in (including dotfiles), then:
git add .
git commit -m "Phase 0: benchmark warehouse + gold edges"
git push -u origin main
```

Adding the certification files:

```
git add warehouse/validate/certify_gold_proc.sql warehouse/validate/run_certification.sql README.md
git commit -m "Phase 0: gold certification procedure + driver"
git push
```

The `.gitignore` keeps `.venv/`, secrets (`.env`, `*.key`, `*.pem`), raw run
dumps, and all markdown except this README out of the repo. Verify before any
commit with `git status` — `.venv/` and stray docs should never appear.

## Notes

- The evaluation dialect is Snowflake; static parsers are invoked with
  `dialect="snowflake"`. A parser that cannot handle a construct (e.g. native
  PIVOT) is recorded as a failure on that model — that is a measured result,
  not an error to be worked around.
- Row counts in seed data are irrelevant to lineage; only column structure
  matters. Seeds are intentionally tiny.
- All identifiers, the domain, and the schema are invented for this benchmark.
