"""
Shared utilities for the lineage extraction arms.

All arms emit predicted edges in the SAME normalized schema as the gold set so
the scorer can compare directly:

    {"target_model": str, "target_column": str,
     "source_table": str, "source_column": str}

Notes on normalization:
- identifiers are lowercased and stripped of surrounding double quotes
- Snowflake dynamic-PIVOT output columns arrive with SINGLE QUOTES as part of
  the identifier (e.g. 'Widgets'); we preserve a cleaned form and also mark
  such columns so the scorer can map them to the gold __PIVOTED__ sentinel
- edge TYPE (DIRECT/INDIRECT) is not emitted by static parsers here; the
  scorer treats a missing type as "unspecified" and can score edges either
  type-agnostically or type-aware
"""

import json
import os
import re
import glob

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WAREHOUSE_DIR = os.path.join(REPO_ROOT, "warehouse")
GOLD_PATH = os.path.join(REPO_ROOT, "gold", "gold_edges.json")
RESULTS_DIR = os.path.join(REPO_ROOT, "results")

# SQL files that define transform models (order matters for parsers that
# resolve upstream columns). Bronze is sources only (no lineage) but is parsed
# so column definitions of source tables are known.
SQL_FILES = [
    os.path.join(WAREHOUSE_DIR, "bronze", "bronze.sql"),
    os.path.join(WAREHOUSE_DIR, "silver", "silver.sql"),
    os.path.join(WAREHOUSE_DIR, "gold", "gold.sql"),
    os.path.join(WAREHOUSE_DIR, "gold", "dp_wide.sql"),
    os.path.join(WAREHOUSE_DIR, "gold", "dp_calc.sql"),
]

# The 14 transform models we score (exclude the 6 bronze source tables).
TRANSFORM_MODELS = [
    "stg_products", "stg_orders_unified", "stg_order_items_enriched",
    "stg_events_flat", "stg_sales_base",
    "dim_customer_metrics", "fct_sales_wide_static", "fct_category_pivot_dyn",
    "mart_seller_scorecard", "rpt_exec_summary",
    "dp_party_360", "dp_item_catalog", "dp_settlement_summary",
    "dp_order_economics",
]


def clean_ident(name):
    """Lowercase, strip surrounding double quotes and whitespace. Preserve
    single quotes (they signal dynamic-pivot columns) but flag separately."""
    if name is None:
        return None
    n = name.strip()
    if len(n) >= 2 and n[0] == '"' and n[-1] == '"':
        n = n[1:-1]
    return n.lower()


def is_pivot_style_column(name):
    """Dynamic-pivot output columns keep single quotes in the identifier."""
    if name is None:
        return False
    return "'" in name


def read_sql_files():
    """Return list of (path, text) for all warehouse SQL files that exist."""
    out = []
    for p in SQL_FILES:
        if os.path.exists(p):
            with open(p, "r") as f:
                out.append((p, f.read()))
        else:
            print(f"  [warn] missing SQL file: {p}")
    return out


def split_statements(sql_text):
    """Split into statements on semicolons. Strips both full-line comments and
    trailing inline comments (-- ...) so a comment after a ';' does not bleed
    into the next statement."""
    cleaned_lines = []
    for line in sql_text.splitlines():
        # strip trailing inline comment, but not inside quotes (our SQL has no
        # -- inside string literals, so a simple split is safe here)
        if "--" in line:
            line = line[:line.index("--")]
        if line.strip():
            cleaned_lines.append(line)
    joined = "\n".join(cleaned_lines)
    stmts = [s.strip() for s in joined.split(";") if s.strip()]
    return stmts


def extract_target_model(create_stmt):
    """Pull the created object name from a CREATE TABLE/VIEW ... statement."""
    m = re.search(
        r"create\s+or\s+replace\s+(?:table|view)\s+([a-zA-Z0-9_\"\.]+)",
        create_stmt, re.IGNORECASE)
    if not m:
        return None
    name = m.group(1)
    # take the last dotted part, strip quotes
    name = name.split(".")[-1]
    return clean_ident(name)


def normalize_edge(target_model, target_column, source_table, source_column):
    return {
        "target_model": clean_ident(target_model),
        "target_column": clean_ident(target_column),
        "source_table": clean_ident(source_table),
        "source_column": clean_ident(source_column),
    }


def dedupe_edges(edges):
    seen = set()
    out = []
    for e in edges:
        key = (e["target_model"], e["target_column"],
               e["source_table"], e["source_column"])
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out


def write_results(arm_name, edges, meta):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    payload = {"arm": arm_name, "meta": meta, "edges": edges}
    out_path = os.path.join(RESULTS_DIR, f"{arm_name}_edges.json")
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"  wrote {len(edges)} edges -> {out_path}")
    return out_path


def per_model_counts(edges):
    from collections import Counter
    c = Counter(e["target_model"] for e in edges)
    return dict(sorted(c.items()))
