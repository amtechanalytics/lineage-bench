"""
Scoring harness for the lineage benchmark.

Compares each arm's predicted edges (results/<arm>_edges.json) against the gold
set (gold/gold_edges.json) and reports:

  * per-transform-type precision / recall / F1
  * overall P/R/F1 with Wilson 95% confidence intervals
  * DIRECT vs INDIRECT recall breakdown
  * per-model edge counts (predicted vs gold)
  * McNemar exact paired test between arms on per-gold-edge recall

Matching:
  An edge is identified by (target_model, target_column, source_table,
  source_column), all lowercased/de-quoted. Static arms do not emit edge TYPE,
  so scoring is TYPE-AGNOSTIC for match purposes; the DIRECT/INDIRECT breakdown
  reports recall computed over the gold's type labels (i.e. of the DIRECT gold
  edges, how many were found; same for INDIRECT).

Dynamic-pivot sentinel:
  Gold represents dynamic-pivot outputs with target_column == "__PIVOTED__"
  because the real Snowflake column names carry data-dependent quoted
  identifiers ('Widgets' etc.). Any predicted edge into the dynamic-pivot model
  whose (source_table, source_column) matches a __PIVOTED__ gold edge is
  credited as a match regardless of its predicted target_column. This is the
  only place target_column is relaxed.

Run:  python harness/score.py
"""

import json
import os
import math
from collections import defaultdict, Counter

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLD_PATH = os.path.join(REPO_ROOT, "gold", "gold_edges.json")
RESULTS_DIR = os.path.join(REPO_ROOT, "results")

ARMS = ["sqlglot", "sqllineage", "llm"]  # llm scored if its file exists

DYNAMIC_PIVOT_MODEL = "fct_category_pivot_dyn"
PIVOT_SENTINEL = "__pivoted__"


def norm(s):
    if s is None:
        return None
    return s.strip().lower().strip('"')


def load_gold():
    g = json.load(open(GOLD_PATH))
    edges = []
    for e in g["edges"]:
        edges.append({
            "target_model": norm(e["target_model"]),
            "target_column": norm(e["target_column"]),
            "source_table": norm(e["source_table"]),
            "source_column": norm(e["source_column"]),
            "type": e.get("type", "DIRECT"),
            "transform_type": e.get("transform_type", "UNK"),
        })
    return edges


def load_arm(arm):
    path = os.path.join(RESULTS_DIR, f"{arm}_edges.json")
    if not os.path.exists(path):
        return None
    payload = json.load(open(path))
    preds = set()
    pivot_preds = set()  # (source_table, source_column) into the dynamic pivot
    for e in payload["edges"]:
        tm = norm(e["target_model"])
        tc = norm(e["target_column"])
        st = norm(e["source_table"])
        sc = norm(e["source_column"])
        preds.add((tm, tc, st, sc))
        if tm == DYNAMIC_PIVOT_MODEL:
            pivot_preds.add((st, sc))
    return {"preds": preds, "pivot_preds": pivot_preds, "raw": payload["edges"]}


def gold_key(e):
    return (e["target_model"], e["target_column"], e["source_table"], e["source_column"])


def is_matched(gold_edge, arm):
    """Is this gold edge recalled by the arm's predictions?"""
    gk = gold_key(gold_edge)
    if gk in arm["preds"]:
        return True
    # dynamic-pivot sentinel relaxation
    if gold_edge["target_model"] == DYNAMIC_PIVOT_MODEL and \
       gold_edge["target_column"] == PIVOT_SENTINEL:
        if (gold_edge["source_table"], gold_edge["source_column"]) in arm["pivot_preds"]:
            return True
    return False


def wilson_ci(k, n, z=1.96):
    """Wilson score interval for a binomial proportion."""
    if n == 0:
        return (0.0, 0.0, 0.0)
    p = k / n
    denom = 1 + z*z/n
    center = (p + z*z/(2*n)) / denom
    half = (z * math.sqrt(p*(1-p)/n + z*z/(4*n*n))) / denom
    return (p, max(0.0, center - half), min(1.0, center + half))


def build_gold_predicate_set(gold):
    """Set of gold edge keys for precision scoring, plus the pivot source-pairs
    that legitimately match the sentinel."""
    gold_keys = set(gold_key(e) for e in gold
                    if not (e["target_model"] == DYNAMIC_PIVOT_MODEL
                            and e["target_column"] == PIVOT_SENTINEL))
    pivot_src_pairs = set((e["source_table"], e["source_column"]) for e in gold
                          if e["target_model"] == DYNAMIC_PIVOT_MODEL
                          and e["target_column"] == PIVOT_SENTINEL)
    return gold_keys, pivot_src_pairs


def pred_is_correct(pred, gold_keys, pivot_src_pairs):
    """For precision: is this predicted edge a true positive?"""
    tm, tc, st, sc = pred
    if (tm, tc, st, sc) in gold_keys:
        return True
    if tm == DYNAMIC_PIVOT_MODEL and (st, sc) in pivot_src_pairs:
        return True
    return False


def score_arm(arm_name, arm, gold):
    gold_keys, pivot_src_pairs = build_gold_predicate_set(gold)

    # RECALL: over gold edges
    recalled = [is_matched(e, arm) for e in gold]
    n_gold = len(gold)
    n_recalled = sum(recalled)

    # PRECISION: over predicted edges (only those targeting scored models)
    scored_models = set(e["target_model"] for e in gold)
    preds_scored = [p for p in arm["preds"] if p[0] in scored_models]
    tp = sum(1 for p in preds_scored
             if pred_is_correct(p, gold_keys, pivot_src_pairs))
    n_pred = len(preds_scored)

    prec, prec_lo, prec_hi = wilson_ci(tp, n_pred)
    rec, rec_lo, rec_hi = wilson_ci(n_recalled, n_gold)
    f1 = (2*prec*rec/(prec+rec)) if (prec+rec) > 0 else 0.0

    # per-transform-type recall
    by_type = defaultdict(lambda: [0, 0])  # [recalled, total]
    for e, r in zip(gold, recalled):
        by_type[e["transform_type"]][0] += int(r)
        by_type[e["transform_type"]][1] += 1

    # DIRECT/INDIRECT recall
    by_dir = defaultdict(lambda: [0, 0])
    for e, r in zip(gold, recalled):
        by_dir[e["type"]][0] += int(r)
        by_dir[e["type"]][1] += 1

    return {
        "arm": arm_name,
        "precision": prec, "precision_ci": (prec_lo, prec_hi), "tp": tp, "n_pred": n_pred,
        "recall": rec, "recall_ci": (rec_lo, rec_hi), "n_recalled": n_recalled, "n_gold": n_gold,
        "f1": f1,
        "by_type": {k: {"recalled": v[0], "total": v[1],
                        "recall": v[0]/v[1] if v[1] else 0.0}
                    for k, v in sorted(by_type.items())},
        "by_direction": {k: {"recalled": v[0], "total": v[1],
                             "recall": v[0]/v[1] if v[1] else 0.0}
                         for k, v in sorted(by_dir.items())},
        "recalled_vector": recalled,  # for McNemar
    }


def compute_raw_leaves(gold):
    """Raw/physical source tables = tables that appear as a source but never as
    a target model. Transitive closure terminates at these."""
    sources = set(e["source_table"] for e in gold)
    targets = set(e["target_model"] for e in gold)
    return sources - targets


def transitive_closure(edges, raw_leaves):
    """Compose immediate edges into end-to-end traces terminating at raw
    leaves. Returns a set of (target_model, target_column, raw_table,
    raw_column) tuples: the full trace from each model column back to every
    ultimate raw column that feeds it."""
    fwd = defaultdict(list)
    for (tm, tc, st, sc) in edges:
        fwd[(tm, tc)].append((st, sc))

    closure = set()

    def trace(model, column, origin, visited):
        node = (model, column)
        if node in visited:
            return
        visited = visited | {node}
        for (st, sc) in fwd.get(node, []):
            if st in raw_leaves:
                closure.add((origin[0], origin[1], st, sc))
            else:
                trace(st, sc, origin, visited)

    targets = set((tm, tc) for (tm, tc, _, _) in edges)
    for (tm, tc) in targets:
        trace(tm, tc, (tm, tc), frozenset())
    return closure


def score_closure(arm_name, arm, gold, raw_leaves):
    """End-to-end trace accuracy: does the arm's transitive closure recover the
    gold's closure (model column -> ultimate raw column)?"""
    gold_edge_tuples = [(e["target_model"], e["target_column"],
                         e["source_table"], e["source_column"]) for e in gold]
    gold_closure = transitive_closure(gold_edge_tuples, raw_leaves)
    arm_closure = transitive_closure(list(arm["preds"]), raw_leaves)

    scored_models = set(e["target_model"] for e in gold)
    gold_closure = {t for t in gold_closure if t[0] in scored_models}
    arm_closure = {t for t in arm_closure if t[0] in scored_models}

    tp = len(gold_closure & arm_closure)
    n_gold = len(gold_closure)
    n_pred = len(arm_closure)
    prec, prec_lo, prec_hi = wilson_ci(tp, n_pred)
    rec, rec_lo, rec_hi = wilson_ci(tp, n_gold)
    f1 = (2*prec*rec/(prec+rec)) if (prec+rec) > 0 else 0.0
    return {
        "arm": arm_name,
        "trace_precision": prec, "trace_precision_ci": (prec_lo, prec_hi),
        "trace_recall": rec, "trace_recall_ci": (rec_lo, rec_hi),
        "trace_f1": f1, "tp": tp, "n_gold_traces": n_gold, "n_pred_traces": n_pred,
    }


def mcnemar(vec_a, vec_b):
    """Exact McNemar on paired binary recall vectors. Returns (b, c, p_value)
    where b = a-correct/b-wrong, c = a-wrong/b-correct."""
    b = sum(1 for x, y in zip(vec_a, vec_b) if x and not y)
    c = sum(1 for x, y in zip(vec_a, vec_b) if y and not x)
    n = b + c
    if n == 0:
        return b, c, 1.0
    # exact binomial two-sided p
    from math import comb
    k = min(b, c)
    p = sum(comb(n, i) for i in range(0, k+1)) * (0.5 ** n) * 2
    return b, c, min(1.0, p)


def fmt_pct(x):
    return f"{100*x:5.1f}%"


def main():
    gold = load_gold()
    print(f"Gold: {len(gold)} edges, "
          f"{sum(1 for e in gold if e['type']=='DIRECT')} DIRECT / "
          f"{sum(1 for e in gold if e['type']=='INDIRECT')} INDIRECT\n")

    scored = {}
    arms_loaded = {}
    for arm_name in ARMS:
        arm = load_arm(arm_name)
        if arm is None:
            print(f"[skip] {arm_name}: no results file")
            continue
        arms_loaded[arm_name] = arm
        scored[arm_name] = score_arm(arm_name, arm, gold)

    if not scored:
        print("No arms scored. Run the extraction arms first.")
        return

    # overall table
    print("=" * 72)
    print(f"{'ARM':<12}{'Precision':<20}{'Recall':<20}{'F1':<8}")
    print("-" * 72)
    for name, s in scored.items():
        p_lo, p_hi = s["precision_ci"]
        r_lo, r_hi = s["recall_ci"]
        print(f"{name:<12}"
              f"{fmt_pct(s['precision'])} [{fmt_pct(p_lo)},{fmt_pct(p_hi)}]  "
              f"{fmt_pct(s['recall'])} [{fmt_pct(r_lo)},{fmt_pct(r_hi)}]  "
              f"{s['f1']:.3f}")
    print("=" * 72)

    # per-transform-type recall matrix
    all_types = sorted({t for s in scored.values() for t in s["by_type"]})
    print("\nPer-transform-type RECALL (recalled/total):")
    header = f"{'type':<8}" + "".join(f"{n:<16}" for n in scored)
    print(header)
    for t in all_types:
        row = f"{t:<8}"
        for name, s in scored.items():
            d = s["by_type"].get(t, {"recalled": 0, "total": 0, "recall": 0})
            row += f"{fmt_pct(d['recall'])} ({d['recalled']}/{d['total']})".ljust(16)
        print(row)

    # DIRECT/INDIRECT
    print("\nDIRECT / INDIRECT recall:")
    for name, s in scored.items():
        parts = []
        for dtype, d in s["by_direction"].items():
            parts.append(f"{dtype}={fmt_pct(d['recall'])} ({d['recalled']}/{d['total']})")
        print(f"  {name:<12} " + "  ".join(parts))

    # McNemar pairwise
    names = list(scored.keys())
    if len(names) >= 2:
        print("\nMcNemar exact (paired recall), per model-pair:")
        for i in range(len(names)):
            for j in range(i+1, len(names)):
                a, b = names[i], names[j]
                bb, cc, p = mcnemar(scored[a]["recalled_vector"],
                                    scored[b]["recalled_vector"])
                sig = "*" if p < 0.05 else " "
                print(f"  {a} vs {b}: b={bb} c={cc} p={p:.4f} {sig}")

    # near-miss diagnostics: for each arm, gold edges missed where the arm DID
    # predict something for that (target_model, target_column) but with a
    # different source -- reveals alias/naming mismatches vs true failures.
    print("\nNear-miss diagnostics (missed gold edge, but arm predicted same "
          "target col with a DIFFERENT source):")
    for arm_name, arm in arms_loaded.items():
        pred_by_target = defaultdict(set)
        for (tm, tc, st, sc) in arm["preds"]:
            pred_by_target[(tm, tc)].add((st, sc))
        near = 0
        examples = []
        for e in gold:
            if is_matched(e, arm):
                continue
            key = (e["target_model"], e["target_column"])
            if key in pred_by_target:
                near += 1
                if len(examples) < 6:
                    got = list(pred_by_target[key])[:2]
                    examples.append(
                        f"{e['target_model']}.{e['target_column']}: "
                        f"gold<-{e['source_table']}.{e['source_column']} "
                        f"| arm<-{got}")
        print(f"  {arm_name}: {near} near-misses")
        for ex in examples:
            print(f"      {ex}")

    # ---- secondary metric: transitive-closure (end-to-end trace) ----
    raw_leaves = compute_raw_leaves(gold)
    print("\n" + "=" * 72)
    print("SECONDARY: end-to-end trace accuracy (transitive closure to raw)")
    print(f"(raw leaves: {len(raw_leaves)} physical tables; "
          f"traces composed through intermediate models)")
    print("-" * 72)
    print(f"{'ARM':<12}{'Trace-Prec':<20}{'Trace-Rec':<20}{'Trace-F1':<8}")
    print("-" * 72)
    closure_out = {}
    for arm_name, arm in arms_loaded.items():
        c = score_closure(arm_name, arm, gold, raw_leaves)
        closure_out[arm_name] = c
        tp_lo, tp_hi = c["trace_precision_ci"]
        tr_lo, tr_hi = c["trace_recall_ci"]
        print(f"{arm_name:<12}"
              f"{fmt_pct(c['trace_precision'])} [{fmt_pct(tp_lo)},{fmt_pct(tp_hi)}]  "
              f"{fmt_pct(c['trace_recall'])} [{fmt_pct(tr_lo)},{fmt_pct(tr_hi)}]  "
              f"{c['trace_f1']:.3f}")
        print(f"{'':12}(gold traces={c['n_gold_traces']}, "
              f"arm traces={c['n_pred_traces']}, matched={c['tp']})")
    print("=" * 72)
    print("Note: an arm can score high on immediate per-hop F1 yet low on trace")
    print("F1 if it fails one critical hop that poisons every path through it.")

    # write machine-readable summary
    out = {name: {k: v for k, v in s.items() if k != "recalled_vector"}
           for name, s in scored.items()}
    for name in out:
        if name in closure_out:
            out[name]["closure"] = closure_out[name]
    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(os.path.join(RESULTS_DIR, "scores.json"), "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nWrote results/scores.json")


if __name__ == "__main__":
    main()
