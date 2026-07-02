"""
SQLGlot static lineage arm.

Reads the warehouse SQL, and for every column of every transform model asks
sqlglot.lineage to trace that column back to its source table+column(s). Emits
normalized edges to results/sqlglot_edges.json.

Design choices tuned to this benchmark:
- dialect="snowflake" so PIVOT / FLATTEN / VARIANT paths parse
- each model wrapped in try/except: an unparseable model is RECORDED as a
  failure (empty edges + logged reason), not a crash -- a parser that cannot
  handle native PIVOT is a measured result, not an error to hide
- we build a schema/scope of all CREATE statements so upstream columns resolve
- leaf sources are walked from the lineage tree; only edges whose source is a
  real table column are kept (intermediate CTE hops are collapsed to the
  ultimate source, matching how the gold is defined)

Run:  python arms/run_sqlglot.py
"""

import sys
import traceback
from collections import defaultdict

import arm_utils as U

try:
    import sqlglot
    from sqlglot import exp
    from sqlglot.lineage import lineage
    from sqlglot.optimizer.scope import build_scope
except ImportError:
    print("ERROR: sqlglot not installed. Run: pip install -r requirements.txt")
    sys.exit(1)

DIALECT = "snowflake"


def build_schema(statements):
    """Map model/table name -> list of output column names, by parsing each
    CREATE statement. Needed so lineage() can resolve columns across models."""
    schema = {}
    parsed = {}
    for stmt in statements:
        model = U.extract_target_model(stmt)
        if not model:
            continue
        try:
            tree = sqlglot.parse_one(stmt, dialect=DIALECT)
        except Exception:
            continue
        parsed[model] = tree
        cols = []
        # try to read the explicit output column list first (CREATE ... ( ... ))
        # else fall back to projections in the SELECT
        select = tree.find(exp.Select)
        if select:
            for proj in select.expressions:
                alias = proj.alias_or_name
                if alias:
                    cols.append(alias)
        schema[model] = cols
    return schema, parsed


def sources_for_column(model, column, statements_by_model, schema):
    """Use sqlglot.lineage to trace one output column of `model` to leaf
    table.column sources. Returns list of (source_table, source_column)."""
    sql = statements_by_model.get(model)
    if sql is None:
        return [], "no-sql-for-model"
    try:
        node = lineage(column, sql, schema=schema, dialect=DIALECT)
    except Exception as e:
        return [], f"lineage-error: {type(e).__name__}: {e}"

    out = []
    # walk the lineage DAG; leaf nodes carry a table source
    for n in node.walk():
        # a leaf has no downstream and references a physical column
        tbl = None
        col = None
        try:
            if isinstance(n.expression, exp.Column):
                col = n.expression.name
                tbl = n.expression.table
            # some nodes carry source table in n.source
            if not tbl and getattr(n, "source", None) is not None:
                if isinstance(n.source, exp.Table):
                    tbl = n.source.name
        except Exception:
            continue
        if tbl and col:
            out.append((tbl, col))
    return out, None


def main():
    print("== SQLGlot static lineage arm ==")
    files = U.read_sql_files()
    all_statements = []
    for path, text in files:
        for stmt in U.split_statements(text):
            if stmt.lower().startswith("create"):
                all_statements.append(stmt)
    print(f"  parsed {len(all_statements)} CREATE statements")

    schema, parsed = build_schema(all_statements)
    statements_by_model = {U.extract_target_model(s): s for s in all_statements
                           if U.extract_target_model(s)}

    edges = []
    failures = {}
    for model in U.TRANSFORM_MODELS:
        cols = schema.get(model, [])
        if not cols:
            failures[model] = "no-columns-resolved"
            print(f"  [fail] {model}: no columns resolved")
            continue
        model_edges = 0
        model_errs = []
        for col in cols:
            srcs, err = sources_for_column(model, col, statements_by_model, schema)
            if err:
                model_errs.append(f"{col}:{err}")
                continue
            for (stbl, scol) in srcs:
                if stbl and stbl.lower() == model.lower():
                    continue  # self-reference, skip
                edges.append(U.normalize_edge(model, col, stbl, scol))
                model_edges += 1
        if model_errs:
            failures[model] = "; ".join(model_errs[:5])
        print(f"  {model}: {model_edges} edges"
              + (f"  ({len(model_errs)} col errors)" if model_errs else ""))

    edges = U.dedupe_edges(edges)
    meta = {
        "dialect": DIALECT,
        "sqlglot_version": sqlglot.__version__,
        "statements": len(all_statements),
        "failures": failures,
        "per_model_counts": U.per_model_counts(edges),
    }
    U.write_results("sqlglot", edges, meta)
    print(f"  total edges: {len(edges)}; models with issues: {len(failures)}")


if __name__ == "__main__":
    main()
