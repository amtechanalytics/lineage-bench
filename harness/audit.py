"""
Audit the 'too clean' cells: for a given arm and transform type, print the
GOLD edges and show exactly which predicted edge matched each one, so we can
confirm perfect-recall cells are genuine edge-for-edge matches and not an
artifact of loose sentinel/multi-source matching.

Run:  python harness/audit.py <arm> <transform_type>
Example: python harness/audit.py llm_sonnet T5b
         python harness/audit.py llm_sonnet T7
         python harness/audit.py llm_haiku T5b
"""

import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLD_PATH = os.path.join(REPO_ROOT, "gold", "gold_edges.json")
RESULTS_DIR = os.path.join(REPO_ROOT, "results")

DYNAMIC_PIVOT_MODEL = "fct_category_pivot_dyn"
PIVOT_SENTINEL = "__pivoted__"


def norm(s):
    return s.strip().lower().strip('"') if s else s


def load_gold():
    g = json.load(open(GOLD_PATH))
    return [{
        "target_model": norm(e["target_model"]),
        "target_column": norm(e["target_column"]),
        "source_table": norm(e["source_table"]),
        "source_column": norm(e["source_column"]),
        "type": e.get("type", "DIRECT"),
        "transform_type": e.get("transform_type", "UNK"),
    } for e in g["edges"]]


def load_arm(arm):
    path = os.path.join(RESULTS_DIR, f"{arm}_edges.json")
    payload = json.load(open(path))
    preds = set()
    pivot_preds = set()
    for e in payload["edges"]:
        tm, tc = norm(e["target_model"]), norm(e["target_column"])
        st, sc = norm(e["source_table"]), norm(e["source_column"])
        preds.add((tm, tc, st, sc))
        if tm == DYNAMIC_PIVOT_MODEL:
            pivot_preds.add((st, sc))
    return preds, pivot_preds


def main():
    if len(sys.argv) < 3:
        print("usage: python harness/audit.py <arm> <transform_type>")
        sys.exit(1)
    arm, ttype = sys.argv[1], sys.argv[2]
    gold = load_gold()
    preds, pivot_preds = load_arm(arm)

    rows = [e for e in gold if e["transform_type"] == ttype]
    print(f"Auditing {arm} on {ttype}: {len(rows)} gold edges\n")

    exact, sentinel, missed = 0, 0, 0
    for e in rows:
        gk = (e["target_model"], e["target_column"], e["source_table"], e["source_column"])
        if gk in preds:
            exact += 1
            status = "EXACT MATCH"
        elif (e["target_model"] == DYNAMIC_PIVOT_MODEL and
              e["target_column"] == PIVOT_SENTINEL and
              (e["source_table"], e["source_column"]) in pivot_preds):
            sentinel += 1
            status = "SENTINEL MATCH (pivot col name relaxed)"
        else:
            missed += 1
            status = "MISSED"
        print(f"  [{status}] {e['target_model']}.{e['target_column']} "
              f"<- {e['source_table']}.{e['source_column']} [{e['type']}]")

    print(f"\nSummary: {exact} exact, {sentinel} sentinel-relaxed, {missed} missed")
    if sentinel > 0:
        print(f"NOTE: {sentinel} matches relied on the __PIVOTED__ sentinel "
              f"relaxation (legitimate for dynamic-pivot columns whose names are "
              f"data-dependent, but worth stating explicitly in the paper).")
    if missed == 0 and sentinel == 0:
        print("All matches are EXACT edge-for-edge. Perfect recall is genuine.")


if __name__ == "__main__":
    main()
