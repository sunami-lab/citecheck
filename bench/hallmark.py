"""Score citecheck on the HALLMARK benchmark (github.com/rpatrik96/hallmark).

    git clone --depth 1 https://github.com/rpatrik96/hallmark
    python3 bench/hallmark.py make  hallmark/data/v1.2/test_public.jsonl test.bib
    python3 scripts/citecheck.py test.bib --json test.json
    python3 bench/hallmark.py score hallmark/data/v1.2/test_public.jsonl test.json
"""
import collections
import json
import sys


def make(split, out):
    with open(out, "w", encoding="utf-8") as fh:
        for line in open(split, encoding="utf-8"):
            r = json.loads(line)
            body = ",\n".join(f"  {k} = {{{str(v).replace('{', '').replace('}', '')}}}" for k, v in r["fields"].items())
            fh.write(f"@{r['bibtex_type']}{{{r['bibtex_key']},\n{body}\n}}\n\n")


def score(split, report):
    truth = {json.loads(l)["bibtex_key"]: json.loads(l) for l in open(split, encoding="utf-8")}
    pred = {r["key"]: r["verdict"] for r in json.load(open(report, encoding="utf-8"))["results"]}
    print(f"entries {len(truth)}, predictions {len(pred)}, verdicts {dict(collections.Counter(pred.values()))}")
    for mode, positive in (("strict", {"NOT_FOUND", "MISMATCH"}), ("lenient", {"NOT_FOUND", "MISMATCH", "CHECK"})):
        tp = fp = fn = tn = 0
        for k, t in truth.items():
            flagged = pred.get(k) in positive
            hall = t["label"] == "HALLUCINATED"
            tp += flagged and hall
            fp += flagged and not hall
            fn += hall and not flagged
            tn += not hall and not flagged
        dr, fpr = tp / (tp + fn), fp / (fp + tn)
        prec = tp / (tp + fp) if tp + fp else 0
        f1 = 2 * prec * dr / (prec + dr) if prec + dr else 0
        print(f"{mode:8s} DR {dr:.3f}  FPR {fpr:.3f}  precision {prec:.3f}  F1 {f1:.3f}   (tp {tp} fp {fp} fn {fn} tn {tn})")
    by_type = collections.defaultdict(collections.Counter)
    for k, t in truth.items():
        by_type[t.get("hallucination_type") or "VALID"][pred.get(k, "none")] += 1
    print("\nper type (verdict counts):")
    for typ, c in sorted(by_type.items(), key=lambda x: -sum(x[1].values())):
        n = sum(c.values())
        hit = c["NOT_FOUND"] + c["MISMATCH"]
        print(f"  {typ:24s} n={n:4d}  strict-flagged {hit / n:5.2f}  +CHECK {(hit + c['CHECK']) / n:5.2f}   {dict(c)}")


if __name__ == "__main__":
    {"make": make, "score": score}[sys.argv[1]](*sys.argv[2:])
