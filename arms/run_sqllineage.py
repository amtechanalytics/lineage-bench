"""
sqllineage static lineage arm.

Second static parser, by design different from sqlglot (sqllineage uses its own
analyzer over sqlfluff/sqlparse). Comparing two real static tools avoids the
strawman objection: "static analysis fails on PIVOT" must not be blamable on
one weak parser.

sqllineage's native API is table-level by default; column-level lineage is
available via LineageRunner(...).get_column_lineage(). We invoke it per SQL
file and collect column edges, then normalize to the benchmark schema.

Design choices:
- dialect passed as "snowflake" (via sqlfluff dialect) where supported; if the
  installed sqllineage rejects the dialect for a construct, that model's edges
  are recorded as empty with the error logged -- again, a measured failure
- sqllineage may not resolve some Snowflake-specific constructs (PIVOT ANY,
  LATERAL FLATTEN, VARIANT paths); those show up as gaps, which is the point

Run:  python arms/run_sqllineage.py
"""

import sys
import traceback

import arm_utils as U

try:
    from sqllineage.runner import LineageRunner
except ImportError:
    print("ERROR: sqllineage not installed. Run: pip install -r requirements.txt")
    sys.exit(1)

DIALECT = "snowflake"


def edges_from_file(path, text):
    """Run sqllineage over one file's SQL and return normalized column edges."""
    edges = []
    errors = []
    # sqllineage prefers analyzing the whole script; column lineage needs the
    # non-validating dialect set. Try snowflake, fall back to ansi.
    for dialect in (DIALECT, "ansi", None):
        try:
            kwargs = {"dialect": dialect} if dialect else {}
            runner = LineageRunner(text, **kwargs)
            col_lineage = runner.get_column_lineage()
            for chain in col_lineage:
                # each chain is a tuple of Column objects from source -> target
                if len(chain) < 2:
                    continue
                source_col = chain[0]
                target_col = chain[-1]
                # Column objects expose .parent (table) and .raw_name
                stbl = _table_of(source_col)
                scol = _name_of(source_col)
                ttbl = _table_of(target_col)
                tcol = _name_of(target_col)
                if ttbl and tcol and stbl and scol:
                    edges.append(U.normalize_edge(ttbl, tcol, stbl, scol))
            return edges, errors  # success on this dialect
        except Exception as e:
            errors.append(f"dialect={dialect}: {type(e).__name__}: {e}")
            continue
    return edges, errors


def _table_of(col_obj):
    try:
        parent = getattr(col_obj, "parent", None)
        if parent is not None:
            # parent is a Table; use its raw name last segment
            raw = str(parent)
            return raw.split(".")[-1]
    except Exception:
        pass
    return None


def _name_of(col_obj):
    try:
        return getattr(col_obj, "raw_name", None) or str(col_obj).split(".")[-1]
    except Exception:
        return None


def main():
    print("== sqllineage static lineage arm ==")
    files = U.read_sql_files()
    edges = []
    failures = {}
    for path, text in files:
        fname = path.split("/")[-1]
        try:
            file_edges, errs = edges_from_file(path, text)
            # keep only edges whose target is a scored transform model
            kept = [e for e in file_edges
                    if e["target_model"] in set(U.TRANSFORM_MODELS)]
            edges.extend(kept)
            print(f"  {fname}: {len(kept)} edges kept"
                  + (f"  ({len(errs)} dialect attempts logged)" if errs else ""))
            if errs and not kept:
                failures[fname] = errs[-1]
        except Exception as e:
            failures[fname] = f"{type(e).__name__}: {e}"
            print(f"  [fail] {fname}: {e}")

    edges = U.dedupe_edges(edges)

    # report which scored models got zero edges (the interesting gaps)
    covered = set(e["target_model"] for e in edges)
    missing = [m for m in U.TRANSFORM_MODELS if m not in covered]

    try:
        import sqllineage as _s
        ver = getattr(_s, "__version__", "unknown")
    except Exception:
        ver = "unknown"

    meta = {
        "dialect": DIALECT,
        "sqllineage_version": ver,
        "failures": failures,
        "models_with_zero_edges": missing,
        "per_model_counts": U.per_model_counts(edges),
    }
    U.write_results("sqllineage", edges, meta)
    print(f"  total edges: {len(edges)}")
    if missing:
        print(f"  models with ZERO edges ({len(missing)}): {', '.join(missing)}")


if __name__ == "__main__":
    main()
