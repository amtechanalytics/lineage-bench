"""
SQLGlot static lineage arm (corrected API usage).

Key mechanics:
- schema is built as the NESTED mapping sqlglot expects:
      {table_name: {column_name: "TYPE", ...}, ...}
  for the physical/bronze source tables (parsed from their CREATE TABLE column
  definitions).
- transform models are passed to lineage() via `sources={name: select_sql}` so
  the lineage walk can cross model boundaries down to leaf tables.
- non-lineage statements (USE ..., CREATE STAGE ...) are skipped.
- each column wrapped in try/except: an unresolvable column is a RECORDED
  failure, not a crash. A model whose construct sqlglot cannot handle (e.g.
  dynamic PIVOT) shows up as columns with errors -> measured degradation.

Run:  python arms/run_sqlglot.py
"""

import sys

import arm_utils as U

try:
    import sqlglot
    from sqlglot import exp
    from sqlglot.lineage import lineage
except ImportError:
    print("ERROR: sqlglot not installed. Run: pip install -r requirements.txt")
    sys.exit(1)

DIALECT = "snowflake"


def build_source_schema(statements):
    """Parse CREATE TABLE (col TYPE, ...) definitions into the nested schema
    shape sqlglot wants: {table: {col: TYPE}}."""
    schema = {}
    for stmt in statements:
        try:
            tree = sqlglot.parse_one(stmt, dialect=DIALECT)
        except Exception:
            continue
        if not isinstance(tree, exp.Create):
            continue
        table = tree.this
        if not isinstance(table, exp.Schema):
            continue
        tbl_name = U.clean_ident(table.this.name)
        cols = {}
        for col_def in table.expressions:
            if isinstance(col_def, exp.ColumnDef):
                cols[U.clean_ident(col_def.this.name)] = (
                    col_def.kind.sql(dialect=DIALECT) if col_def.kind else "UNKNOWN")
        if cols:
            schema[tbl_name] = cols
    return schema


def select_body(stmt):
    """Return the CREATE ... AS <select> body as SQL text for one statement."""
    try:
        tree = sqlglot.parse_one(stmt, dialect=DIALECT)
    except Exception:
        return None
    if not isinstance(tree, exp.Create):
        return None
    expr = tree.expression
    if expr is None:
        return None
    return expr.sql(dialect=DIALECT)


def output_columns(stmt):
    """Best-effort list of output column names for a CREATE ... AS SELECT."""
    try:
        tree = sqlglot.parse_one(stmt, dialect=DIALECT)
    except Exception:
        return []
    select = tree.find(exp.Select)
    if not select:
        return []
    cols = []
    for proj in select.expressions:
        name = proj.alias_or_name
        if name and name != "*":
            cols.append(name)
    return cols


def leaf_sources(node, model_name):
    """Walk a lineage node's DAG and collect leaf (table, column) pairs."""
    out = []
    for n in node.walk():
        expr = n.expression
        try:
            if isinstance(expr, exp.Column):
                col = expr.name
                tbl = expr.table
                if tbl and col:
                    out.append((U.clean_ident(tbl), U.clean_ident(col)))
        except Exception:
            continue
    return out


def main():
    print("== SQLGlot static lineage arm ==")
    files = U.read_sql_files()
    all_statements = []
    for path, text in files:
        for stmt in U.split_statements(text):
            low = stmt.lower()
            if low.startswith("use ") or low.startswith("create stage"):
                continue
            if low.startswith("insert"):
                continue
            all_statements.append(stmt)

    create_stmts = [s for s in all_statements if U.extract_target_model(s)]
    print(f"  parsed {len(create_stmts)} CREATE statements")

    schema = build_source_schema(create_stmts)
    print(f"  source-table schema entries: {len(schema)}")

    sources = {}
    for s in create_stmts:
        model = U.extract_target_model(s)
        body = select_body(s)
        if model and body:
            sources[model] = body

    edges = []
    failures = {}
    for model in U.TRANSFORM_MODELS:
        stmt = next((s for s in create_stmts
                     if U.extract_target_model(s) == model), None)
        if stmt is None:
            failures[model] = "statement-not-found"
            print(f"  [fail] {model}: statement not found")
            continue
        cols = output_columns(stmt)
        if not cols:
            failures[model] = "no-output-columns (likely SELECT * / pivot)"
            print(f"  {model}: 0 edges  (no output columns resolved from text)")
            continue
        model_edges = 0
        errs = []
        for col in cols:
            try:
                node = lineage(col, sources[model], schema=schema,
                               sources=sources, dialect=DIALECT)
            except Exception as e:
                errs.append(f"{col}:{type(e).__name__}")
                continue
            for (stbl, scol) in leaf_sources(node, model):
                if stbl == model:
                    continue
                edges.append(U.normalize_edge(model, col, stbl, scol))
                model_edges += 1
        if errs:
            failures[model] = "; ".join(errs[:8])
        print(f"  {model}: {model_edges} edges"
              + (f"  ({len(errs)} col errors)" if errs else ""))

    edges = U.dedupe_edges(edges)
    meta = {
        "dialect": DIALECT,
        "sqlglot_version": sqlglot.__version__,
        "create_statements": len(create_stmts),
        "schema_tables": sorted(schema.keys()),
        "failures": failures,
        "per_model_counts": U.per_model_counts(edges),
    }
    U.write_results("sqlglot", edges, meta)
    print(f"  total edges: {len(edges)}; models with issues: {len(failures)}")


if __name__ == "__main__":
    main()
