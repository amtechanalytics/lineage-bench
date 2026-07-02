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


def build_full_schema(statements):
    """Schema for EVERY table and model: physical tables from their column
    defs, and each CREATE ... AS SELECT model from its output column names.
    Types are unknown for model columns; sqlglot only needs the names to
    resolve immediate-upstream references."""
    schema = build_source_schema(statements)
    for stmt in statements:
        model = U.extract_target_model(stmt)
        if not model or model in schema:
            continue
        cols = output_columns(stmt)
        if cols:
            schema[model] = {U.clean_ident(c): "UNKNOWN" for c in cols}
    return schema


def build_alias_map(stmt):
    """Build alias/CTE -> base-table maps for one CREATE statement.

    Lineage reports a local name instead of a base table in two cases:
      (a) table aliases in FROM/JOIN: `raw_products p` -> p means raw_products
      (b) CTE names: `WITH web AS (SELECT ... FROM raw_orders_web)` -> web means
          the base table(s) its body reads.
    """
    try:
        tree = sqlglot.parse_one(stmt, dialect=DIALECT)
    except Exception:
        return {}, {}

    alias_map = {}   # table alias -> base table
    cte_bases = {}   # cte name -> set of base tables its body reads

    for tbl in tree.find_all(exp.Table):
        base = U.clean_ident(tbl.name)
        alias = tbl.alias
        if alias:
            alias_map[U.clean_ident(alias)] = base

    for cte in tree.find_all(exp.CTE):
        cte_name = U.clean_ident(cte.alias)
        bases = set()
        body = cte.this
        for tbl in body.find_all(exp.Table):
            b = U.clean_ident(tbl.name)
            b = alias_map.get(b, b)
            bases.add(b)
        cte_bases[cte_name] = bases

    return alias_map, cte_bases


def resolve_source(tbl, alias_map, cte_bases):
    """Resolve a lineage-reported source name to base table(s). A CTE over a
    union resolves to multiple bases."""
    t = U.clean_ident(tbl)
    if t in alias_map:
        return [alias_map[t]]
    if t in cte_bases:
        return sorted(cte_bases[t])
    return [t]


def immediate_sources(node, model_name):
    """First-hop sources from a lineage node: the direct children of the root
    output-column node, whose names carry 'table.column'. With schema-only
    resolution (no sources=), intermediate models are NOT expanded, so these
    children are the immediate upstream table.column -- matching gold
    semantics rather than recursing to ultimate leaves."""
    out = []
    seen = set()

    def collect(n):
        raw = getattr(n, "name", None)
        if raw and "." in raw:
            parts = raw.strip().lower().strip('"').split(".")
            tbl, col = parts[-2], parts[-1]
            key = (tbl, col)
            if key not in seen:
                seen.add(key)
                out.append((U.clean_ident(tbl), U.clean_ident(col)))
            return True
        return False

    for child in (getattr(node, "downstream", None) or []):
        if not collect(child):
            for gc in (getattr(child, "downstream", None) or []):
                collect(gc)
    return out


def debug_probe_multi(create_stmts, schema):
    """Dump lineage trees for one single-hop and one multi-hop column so we can
    confirm immediate-upstream extraction. DEBUG_LINEAGE=1 to enable."""
    import os
    if os.environ.get("DEBUG_LINEAGE") != "1":
        return

    def body_for(model):
        stmt = next((s for s in create_stmts
                     if U.extract_target_model(s) == model), None)
        return select_body(stmt) if stmt else None

    def dump(n, depth=0):
        pad = "    " + "  " * depth
        nm = getattr(n, "name", None)
        downstream = getattr(n, "downstream", None) or []
        print(f"{pad}name={nm!r} downstream={len(downstream)}")
        for c in downstream:
            dump(c, depth + 1)

    for model, col in [("stg_products", "product_sku"),
                       ("stg_order_items_enriched", "product_name")]:
        body = body_for(model)
        if not body:
            continue
        print(f"\n  === DEBUG: {model}.{col} (schema-only) ===")
        try:
            node = lineage(col, body, schema=schema, dialect=DIALECT)
            dump(node)
        except Exception as e:
            import traceback
            print("    probe error:", traceback.format_exc())
        print("  === END DEBUG ===")


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

    # Build a FULL schema of every table/model (their output columns) so that
    # lineage() can resolve column references to their immediate source table
    # WITHOUT expanding intermediate models inline. We deliberately do NOT pass
    # `sources` (which would inline intermediate SELECTs and produce transitive
    # lineage to the ultimate raw leaf). Gold uses IMMEDIATE-upstream semantics:
    # an edge points to the table the SELECT directly reads. Schema-only
    # resolution yields exactly that.
    schema = build_full_schema(create_stmts)
    print(f"  schema tables (all models + sources): {len(schema)}")

    debug_probe_multi(create_stmts, schema)

    edges = []
    failures = {}
    for model in U.TRANSFORM_MODELS:
        stmt = next((s for s in create_stmts
                     if U.extract_target_model(s) == model), None)
        if stmt is None:
            failures[model] = "statement-not-found"
            print(f"  [fail] {model}: statement not found")
            continue
        body = select_body(stmt)
        cols = output_columns(stmt)
        if not cols or body is None:
            failures[model] = "no-output-columns (likely SELECT * / pivot)"
            print(f"  {model}: 0 edges  (no output columns resolved from text)")
            continue
        alias_map, cte_bases = build_alias_map(stmt)
        model_edges = 0
        errs = []
        for col in cols:
            try:
                # schema-only (no sources=) -> immediate upstream
                node = lineage(col, body, schema=schema, dialect=DIALECT)
            except Exception as e:
                errs.append(f"{col}:{type(e).__name__}")
                continue
            for (stbl, scol) in immediate_sources(node, model):
                # resolve table aliases and CTE names to base tables
                for base in resolve_source(stbl, alias_map, cte_bases):
                    if base == model:
                        continue
                    edges.append(U.normalize_edge(model, col, base, scol))
                    model_edges += 1
        if errs:
            failures[model] = "; ".join(errs[:8])
        print(f"  {model}: {model_edges} edges"
              + (f"  ({len(errs)} col errors)" if errs else ""))

    edges = U.dedupe_edges(edges)
    meta = {
        "dialect": DIALECT,
        "sqlglot_version": sqlglot.__version__,
        "lineage_semantics": "immediate-upstream (schema-only resolution)",
        "create_statements": len(create_stmts),
        "schema_tables": sorted(schema.keys()),
        "failures": failures,
        "per_model_counts": U.per_model_counts(edges),
    }
    U.write_results("sqlglot", edges, meta)
    print(f"  total edges: {len(edges)}; models with issues: {len(failures)}")


if __name__ == "__main__":
    main()
