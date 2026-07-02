"""
LLM lineage arm — single clean script with built-in validation + budget guard.

Flow:
  PHASE 1 (VALIDATE): send ONE model definition to each LLM, parse the
    response, and check it matches the exact edge schema we expect. If any
    validation check fails, STOP immediately and report precisely what was
    wrong (no further spend).
  PHASE 2 (FULL RUN): only if validation passed for a model, process all 14
    model definitions with that model.

A hard BUDGET_CEILING aborts either phase if cumulative estimated cost exceeds
it. Default ceiling = estimated cost (~$0.16) + $5 buffer = $5.16.

Prompt caching: the invariant instruction block is cache_control'd, so every
call after the first reuses it (~0.1x input cost on the stable prefix).

Models (both pinned snapshots; see README reproducibility note):
  llm_haiku  = claude-haiku-4-5-20251001   (dated snapshot)
  llm_sonnet = claude-sonnet-4-6           (dateless-but-pinned, 4.6 generation)

Env overrides:
  ANTHROPIC_API_KEY   required (loaded from .env)
  BUDGET_CEILING      USD hard abort (default 5.16)
  LLM_MODELS          comma list to restrict which models run
  RESUME=1            skip models whose results file already exists

Run:  python arms/run_llm.py
"""

import os
import sys
import json
import time

import arm_utils as U

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(U.REPO_ROOT, ".env"))
except ImportError:
    pass

try:
    import anthropic
except ImportError:
    print("ERROR: anthropic SDK not installed. Run: pip install -r requirements.txt")
    sys.exit(1)

# ---- pinned models -------------------------------------------------------
MODELS = {
    "llm_haiku": "claude-haiku-4-5-20251001",
    "llm_sonnet": "claude-sonnet-4-6",
}
if os.environ.get("LLM_MODELS"):
    wanted = set(os.environ["LLM_MODELS"].split(","))
    MODELS = {k: v for k, v in MODELS.items() if k in wanted or v in wanted}

# ---- pricing (USD/token) for the budget guard ----------------------------
PRICE = {
    "claude-haiku-4-5-20251001": {"in": 1.0/1e6, "out": 5.0/1e6, "cache_read": 0.1/1e6, "cache_write": 1.25/1e6},
    "claude-sonnet-4-6":         {"in": 3.0/1e6, "out": 15.0/1e6, "cache_read": 0.3/1e6, "cache_write": 3.75/1e6},
}

# estimated ~$0.16 for both arms; default ceiling adds a $5 buffer on top.
BUDGET_CEILING = float(os.environ.get("BUDGET_CEILING", "5.16"))
RESUME = os.environ.get("RESUME") == "1"
HEARTBEAT = os.path.join(U.RESULTS_DIR, "llm_heartbeat.txt")

SYSTEM_INSTRUCTION = """You extract COLUMN-LEVEL DATA LINEAGE from a single SQL model definition.

Given one `CREATE TABLE|VIEW ... AS SELECT ...` statement, identify, for EACH
output column, which upstream column(s) it derives from.

CRITICAL RULES:
1. IMMEDIATE UPSTREAM ONLY. Report the table the SELECT DIRECTLY reads from
   (its FROM/JOIN/CTE sources), NOT the ultimate raw origin. If the query reads
   from `stg_products`, the source_table is `stg_products` even if stg_products
   itself derives from something else. Do NOT trace through to raw tables.
2. Resolve table ALIASES to the real table name (if `orders o`, use `orders`).
3. For a column built from an EXPRESSION over several columns (e.g.
   `a - b AS net`), emit ONE edge per input column (net<-a, net<-b).
4. Edge TYPE:
   - DIRECT: the output VALUE is computed from the source value (projection,
     rename, arithmetic, CASE result, aggregation input).
   - INDIRECT: the source only INFLUENCES the output via a filter, GROUP BY,
     PARTITION BY, ORDER BY, or join key -- it is not the value itself.
5. For dynamic PIVOT (`PIVOT(... FOR col IN (ANY))`) where output column names
   are data-dependent and not in the SQL text, use the literal target_column
   value "__PIVOTED__" for each pivoted measure edge.
6. Output ONLY valid JSON, no prose, no markdown fences.

OUTPUT FORMAT (a JSON array; each element an edge):
[
  {"target_column": "...", "source_table": "...", "source_column": "...", "type": "DIRECT|INDIRECT"}
]

WORKED EXAMPLE:
Input:
  CREATE VIEW v AS
  SELECT o.id AS order_id, o.amt - o.disc AS net, c.name AS cust
  FROM orders o JOIN customers c ON o.cust_id = c.id;
Output:
  [
    {"target_column":"order_id","source_table":"orders","source_column":"id","type":"DIRECT"},
    {"target_column":"net","source_table":"orders","source_column":"amt","type":"DIRECT"},
    {"target_column":"net","source_table":"orders","source_column":"disc","type":"DIRECT"},
    {"target_column":"cust","source_table":"customers","source_column":"name","type":"DIRECT"},
    {"target_column":"cust","source_table":"orders","source_column":"cust_id","type":"INDIRECT"},
    {"target_column":"cust","source_table":"customers","source_column":"id","type":"INDIRECT"}
  ]
"""


def heartbeat(msg):
    os.makedirs(U.RESULTS_DIR, exist_ok=True)
    with open(HEARTBEAT, "w") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")


def get_model_statements():
    files = U.read_sql_files()
    out = []
    for path, text in files:
        for stmt in U.split_statements(text):
            low = stmt.lower()
            if low.startswith("use ") or low.startswith("create stage") or low.startswith("insert"):
                continue
            model = U.extract_target_model(stmt)
            if model in set(U.TRANSFORM_MODELS):
                out.append((model, stmt + ";"))
    seen, ordered = set(), []
    for m in U.TRANSFORM_MODELS:
        for (mm, s) in out:
            if mm == m and mm not in seen:
                seen.add(mm)
                ordered.append((mm, s))
    return ordered


def call_model(client, model_id, sql):
    """One API call. Returns (raw_text, parsed_or_None, usage, error)."""
    try:
        resp = client.messages.create(
            model=model_id,
            max_tokens=2000,
            system=[{
                "type": "text",
                "text": SYSTEM_INSTRUCTION,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{
                "role": "user",
                "content": f"Extract column lineage for this model:\n\n{sql}",
            }],
        )
    except Exception as e:
        return None, None, None, f"api-error: {type(e).__name__}: {e}"

    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    usage = {
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "cache_read": getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
        "cache_write": getattr(resp.usage, "cache_creation_input_tokens", 0) or 0,
    }
    cleaned = text.strip()
    if cleaned.startswith("```"):
        parts = cleaned.split("```")
        cleaned = parts[1] if len(parts) > 1 else cleaned
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    try:
        parsed = json.loads(cleaned.strip())
    except Exception as e:
        return text, None, usage, f"parse-error: {e}"
    return text, parsed, usage, None


def validate_response(parsed, model_name):
    """Strict schema check on a parsed LLM response. Returns (ok, [problems])."""
    problems = []
    if not isinstance(parsed, list):
        return False, [f"top-level is {type(parsed).__name__}, expected list"]
    if len(parsed) == 0:
        return False, ["empty edge list"]
    required = {"target_column", "source_table", "source_column", "type"}
    for i, e in enumerate(parsed):
        if not isinstance(e, dict):
            problems.append(f"edge[{i}] is {type(e).__name__}, expected object")
            continue
        missing = required - set(e.keys())
        if missing:
            problems.append(f"edge[{i}] missing keys: {sorted(missing)}")
        if e.get("type") not in ("DIRECT", "INDIRECT", None):
            problems.append(f"edge[{i}] type={e.get('type')!r} not DIRECT/INDIRECT")
        for k in ("target_column", "source_table", "source_column"):
            v = e.get(k)
            if v is not None and not isinstance(v, str):
                problems.append(f"edge[{i}].{k} is {type(v).__name__}, expected str")
    return (len(problems) == 0), problems


def cost_of(model_id, usage):
    p = PRICE.get(model_id, {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0})
    return (usage["input_tokens"] * p["in"] + usage["output_tokens"] * p["out"]
            + usage["cache_read"] * p["cache_read"] + usage["cache_write"] * p["cache_write"])


def edges_from_parsed(model, parsed):
    edges = []
    for e in parsed:
        tc, st, sc = e.get("target_column"), e.get("source_table"), e.get("source_column")
        if tc and st and sc:
            edges.append(U.normalize_edge(model, tc, st, sc))
    return edges


def run_one_model_id(client, arm_name, model_id, statements, budget_state):
    """Validate on the first statement, then (if ok) process the rest.
    budget_state = {"spent": float}. Returns (edges, meta)."""
    print(f"\n{'='*64}\n== {arm_name}  ({model_id})\n{'='*64}")

    # ---------- PHASE 1: VALIDATE on the first model ----------
    first_model, first_sql = statements[0]
    print(f"[VALIDATE] sending 1 definition ({first_model}) to check response schema...")
    text, parsed, usage, err = call_model(client, model_id, first_sql)
    if err:
        print(f"[VALIDATE] FAILED: {err}")
        if text is not None:
            print(f"           raw response (first 300 chars): {text[:300]!r}")
        return [], {"aborted": "validation", "reason": err}

    budget_state["spent"] += cost_of(model_id, usage)
    ok, problems = validate_response(parsed, first_model)
    if not ok:
        print(f"[VALIDATE] FAILED — response does not match expected schema:")
        for p in problems[:10]:
            print(f"             - {p}")
        print(f"           raw response (first 400 chars): {text[:400]!r}")
        print(f"           STOPPING before full run. Spend so far: ${budget_state['spent']:.4f}")
        return [], {"aborted": "validation", "problems": problems}

    print(f"[VALIDATE] PASSED — {len(parsed)} well-formed edges from {first_model} "
          f"(in={usage['input_tokens']} out={usage['output_tokens']}"
          + (f" cache_write={usage['cache_write']}" if usage['cache_write'] else "")
          + f") ${cost_of(model_id, usage):.4f}")

    edges = edges_from_parsed(first_model, parsed)
    failures = {}

    # ---------- PHASE 2: FULL RUN on remaining models ----------
    print(f"[FULL RUN] processing remaining {len(statements)-1} definitions...")
    for (model, sql) in statements[1:]:
        if budget_state["spent"] > BUDGET_CEILING:
            print(f"  BUDGET CEILING ${BUDGET_CEILING} exceeded "
                  f"(${budget_state['spent']:.4f}); aborting.")
            return U.dedupe_edges(edges), {"aborted": "budget", "failures": failures}
        text, parsed, usage, err = call_model(client, model_id, sql)
        if err:
            failures[model] = err
            print(f"  [fail] {model}: {err}")
            continue
        budget_state["spent"] += cost_of(model_id, usage)
        ok, problems = validate_response(parsed, model)
        if not ok:
            failures[model] = f"schema: {problems[:3]}"
            print(f"  [warn] {model}: response schema issues {problems[:2]}")
            # still salvage any well-formed edges
        me = edges_from_parsed(model, parsed)
        edges.extend(me)
        cache_note = (f" cache_read={usage['cache_read']}" if usage['cache_read']
                      else (f" cache_write={usage['cache_write']}" if usage['cache_write'] else ""))
        print(f"  {model}: {len(me)} edges  (in={usage['input_tokens']} "
              f"out={usage['output_tokens']}{cache_note}) "
              f"${cost_of(model_id, usage):.4f}  cum=${budget_state['spent']:.4f}")
        heartbeat(f"{arm_name} {model} cum=${budget_state['spent']:.4f}")

    return U.dedupe_edges(edges), {"failures": failures}


def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set (put it in .env or export it).")
        sys.exit(1)

    client = anthropic.Anthropic()
    statements = get_model_statements()
    print(f"Loaded {len(statements)} transform-model definitions.")
    print(f"Models: {list(MODELS.items())}")
    print(f"BUDGET_CEILING=${BUDGET_CEILING}  RESUME={RESUME}")
    print(f"(estimated full cost for both arms ~= $0.16)")

    budget_state = {"spent": 0.0}
    for arm_name, model_id in MODELS.items():
        out_path = os.path.join(U.RESULTS_DIR, f"{arm_name}_edges.json")
        if RESUME and os.path.exists(out_path):
            print(f"[resume] {arm_name}: results exist, skipping.")
            continue
        edges, meta = run_one_model_id(client, arm_name, model_id, statements, budget_state)
        if meta.get("aborted") == "validation":
            print(f"\n*** {arm_name} ABORTED at validation — NOT running full set. ***")
            print(f"*** Fix the prompt/parse issue above and re-run. No further models processed. ***")
            break  # do not proceed to the next model if validation is broken
        meta.update({
            "model_id": model_id,
            "estimated_cost_usd": round(budget_state["spent"], 4),
            "per_model_counts": U.per_model_counts(edges),
        })
        U.write_results(arm_name, edges, meta)
        print(f"  {arm_name} done: {len(edges)} edges")

    print(f"\nTotal estimated spend: ${budget_state['spent']:.4f}")
    print("If both arms completed, run: python harness/score.py")


if __name__ == "__main__":
    main()
