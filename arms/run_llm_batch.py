"""
Batch-mode LLM lineage arm (context-scale probe / future-work teaser).

Instead of one API call per model definition (the primary experiment), this
sends ALL 14 model definitions in a SINGLE prompt and asks for the complete
edge set for the whole warehouse at once. This probes the hypothesis that LLM
lineage accuracy degrades as context grows and cross-statement resolution is
required -- the regime where deterministic static parsers may reassert an
advantage.

IMPORTANT SCOPE NOTE (for the paper): 14 models (~3K tokens) is still a SMALL
context. This probe can only SUGGEST the direction of the context-scale effect;
it cannot establish the large-context regime (40+ models, deep multi-file
chains). Report as a preliminary probe motivating future work, not as proof.

Output arms: llm_haiku_batch, llm_sonnet_batch -> scored by the same harness.

Env:
  ANTHROPIC_API_KEY   required (.env)
  BUDGET_CEILING      USD abort (default 5.16)
  LLM_MODELS          restrict which models run

Run:  python arms/run_llm_batch.py
"""

import os
import sys
import json

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

MODELS = {
    "llm_haiku_batch": "claude-haiku-4-5-20251001",
    "llm_sonnet_batch": "claude-sonnet-4-6",
}
if os.environ.get("LLM_MODELS"):
    wanted = set(os.environ["LLM_MODELS"].split(","))
    MODELS = {k: v for k, v in MODELS.items() if k in wanted or v in wanted}

PRICE = {
    "claude-haiku-4-5-20251001": {"in": 1.0/1e6, "out": 5.0/1e6},
    "claude-sonnet-4-6":         {"in": 3.0/1e6, "out": 15.0/1e6},
}
BUDGET_CEILING = float(os.environ.get("BUDGET_CEILING", "5.16"))

# batch instruction: same rules as per-statement, but now the model must emit a
# target_model field too (since many models are present at once).
BATCH_INSTRUCTION = """You extract COLUMN-LEVEL DATA LINEAGE for an ENTIRE data warehouse at once.

You are given MANY `CREATE TABLE|VIEW ... AS SELECT ...` statements. For EACH
output column of EACH created model, identify which upstream column(s) it
derives from.

CRITICAL RULES:
1. IMMEDIATE UPSTREAM ONLY. Report the table the SELECT DIRECTLY reads from
   (its FROM/JOIN/CTE sources), NOT the ultimate raw origin. If a model reads
   from `stg_products`, the source_table is `stg_products` even though
   stg_products itself derives from raw tables. Do NOT trace transitively.
2. Resolve table ALIASES to the real table name.
3. Expression over several columns -> ONE edge per input column.
4. TYPE: DIRECT = output value computed from source value; INDIRECT = source
   only influences via filter/GROUP BY/PARTITION BY/ORDER BY/join key.
5. Dynamic PIVOT with data-dependent output names -> target_column "__PIVOTED__".
6. Output ONLY a valid JSON array, no prose, no markdown fences.

Each edge MUST include target_model (the created object the column belongs to):
[
  {"target_model":"...","target_column":"...","source_table":"...","source_column":"...","type":"DIRECT|INDIRECT"}
]
"""


def _salvage_edges(partial_json):
    """Extract complete edge objects from a truncated JSON array. Finds every
    balanced {...} object and parses each individually, discarding the final
    incomplete one."""
    import re
    edges = []
    depth = 0
    start = None
    for i, ch in enumerate(partial_json):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                chunk = partial_json[start:i+1]
                try:
                    edges.append(json.loads(chunk))
                except Exception:
                    pass
                start = None
    return edges


def get_all_sql():
    """Concatenate all 14 transform-model definitions into one text block."""
    files = U.read_sql_files()
    stmts = []
    for path, text in files:
        for stmt in U.split_statements(text):
            low = stmt.lower()
            if low.startswith("use ") or low.startswith("create stage") or low.startswith("insert"):
                continue
            if U.extract_target_model(stmt) in set(U.TRANSFORM_MODELS):
                stmts.append(stmt + ";")
    return "\n\n".join(stmts)


def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set.")
        sys.exit(1)
    client = anthropic.Anthropic()
    all_sql = get_all_sql()
    approx_tokens = len(all_sql) // 4
    print(f"Batch context: all 14 models in one prompt (~{approx_tokens} tokens SQL).")
    print(f"Models: {list(MODELS.items())}  BUDGET_CEILING=${BUDGET_CEILING}\n")

    spent = 0.0
    for arm_name, model_id in MODELS.items():
        print(f"== {arm_name} ({model_id}) ==")
        try:
            resp = client.messages.create(
                model=model_id,
                max_tokens=16000,  # whole-warehouse edge set (~206 edges JSON)
                system=[{"type": "text", "text": BATCH_INSTRUCTION,
                         "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user",
                           "content": f"Extract all column lineage:\n\n{all_sql}"}],
            )
        except Exception as e:
            print(f"  [fail] api-error: {type(e).__name__}: {e}")
            continue

        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        p = PRICE[model_id]
        cost = resp.usage.input_tokens * p["in"] + resp.usage.output_tokens * p["out"]
        spent += cost
        print(f"  usage in={resp.usage.input_tokens} out={resp.usage.output_tokens} "
              f"${cost:.4f}  cum=${spent:.4f}")

        cleaned = text.strip()
        if cleaned.startswith("```"):
            parts = cleaned.split("```")
            cleaned = parts[1] if len(parts) > 1 else cleaned
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
        cleaned = cleaned.strip()

        truncated = (resp.stop_reason == "max_tokens")
        try:
            parsed = json.loads(cleaned)
        except Exception as e:
            if truncated:
                # salvage complete edge objects from a truncated array
                parsed = _salvage_edges(cleaned)
                print(f"  [truncated at max_tokens] response cut off; "
                      f"salvaged {len(parsed)} complete edges before the cut. "
                      f"This is itself a batch-mode finding: whole-warehouse "
                      f"lineage overflowed the output budget.")
            else:
                print(f"  [fail] parse-error: {e}; raw[:200]={text[:200]!r}")
                continue

        edges = []
        for e in parsed:
            tm = e.get("target_model")
            tc = e.get("target_column")
            st = e.get("source_table")
            sc = e.get("source_column")
            if tm and tc and st and sc:
                edges.append(U.normalize_edge(tm, tc, st, sc))
        edges = U.dedupe_edges(edges)

        # keep only edges targeting real scored models
        scored = set(U.TRANSFORM_MODELS)
        kept = [e for e in edges if e["target_model"] in scored]
        dropped = len(edges) - len(kept)

        meta = {
            "model_id": model_id, "mode": "batch-single-call",
            "context_tokens_approx": approx_tokens,
            "output_tokens": resp.usage.output_tokens,
            "truncated_at_max_tokens": truncated,
            "estimated_cost_usd": round(cost, 4),
            "edges_targeting_unknown_models": dropped,
            "per_model_counts": U.per_model_counts(kept),
        }
        U.write_results(arm_name, kept, meta)
        print(f"  {arm_name}: {len(kept)} edges kept"
              + (f" ({dropped} dropped: unknown target models)" if dropped else "")
              + "\n")

        if spent > BUDGET_CEILING:
            print(f"  BUDGET CEILING exceeded; stopping."); break

    print(f"Total batch spend: ${spent:.4f}")
    print("Score with: python harness/score.py  (add batch arms to ARMS list first)")


if __name__ == "__main__":
    main()
