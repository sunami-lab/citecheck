"""Evaluate the claims rubric on real citing sentences and on citation swaps.

For each paper, citecheck --claims gives the sentences citing each reference and the reference's
abstract. An ORIGINAL pair is a sentence with the abstract of the work it cites. A SWAP pairs the
same sentence with the abstract of another reference from the same paper's bibliography, which
imitates a real but wrong citation on the same topic. Claude judges shuffled batches with
skills/claims/rubric.md and never sees which pairs are swaps.

    python3 scripts/citecheck.py paper_dir/ --claims paper.claims.json      # per paper
    python3 bench/claims.py make  pairs.jsonl a.claims.json b.claims.json ...
    python3 bench/claims.py judge pairs.jsonl judged.jsonl [--model sonnet]
    python3 bench/claims.py score pairs.jsonl judged.jsonl
"""
import collections
import json
import os
import random
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RUBRIC = os.path.join(HERE, "..", "skills", "claims", "rubric.md")
PER_PAPER = 8  # sentences sampled per paper
LABELS = ("SUPPORTED", "PLAUSIBLE", "UNSUPPORTED", "CONTRADICTED", "UNCHECKED")


def make(out, *claims_files):
    rng = random.Random(0)
    pairs = []
    for path in claims_files:
        refs = [r for r in json.load(open(path, encoding="utf-8")) if r["abstract"] and len(r["abstract"]) > 200
                and r["verdict"] in ("VERIFIED", "CHECK")]
        by_key = {r["key"]: r for r in refs}
        items = [(r["key"], c["sentence"]) for r in refs for c in r["contexts"]
                 if 40 <= len(c["sentence"]) <= 600 and len(re.findall(r"\[[^\]]+\]", c["sentence"])) <= 3
                 and len(re.findall(r"[A-Za-z]{3,}", c["sentence"])) >= 8]  # skip equation fragments
        rng.shuffle(items)
        paper = os.path.basename(path)
        for key, sentence in items[:PER_PAPER]:
            cited_here = {k.strip(" []") for group in re.findall(r"\[([^\]]+)\]", sentence) for k in group.split(",")}
            others = [k for k in by_key if k not in cited_here | {key}]
            if not others:
                continue
            decoy = rng.choice(others)
            for kind, source in (("ORIGINAL", key), ("SWAP", decoy)):
                pairs.append({"paper": paper, "kind": kind, "target": key, "sentence": sentence,
                              "abstract_of": source, "abstract": by_key[source]["abstract"][:2500]})
    rng.shuffle(pairs)
    with open(out, "w", encoding="utf-8") as fh:
        for i, p in enumerate(pairs):
            fh.write(json.dumps(dict(p, id=i), ensure_ascii=False) + "\n")
    print(f"{len(pairs)} pairs from {len(claims_files)} papers -> {out}")


def _prompt(batch):
    rubric = open(RUBRIC, encoding="utf-8").read()
    items = "\n\n".join(f"### Pair {p['id']}\nCitation being judged: [{p['target']}]\nSentence: {p['sentence']}\n"
                        f"Abstract of the cited work: {p['abstract']}" for p in batch)
    return (f"{rubric}\n\nJudge each pair below with this rubric. Reply with only a JSON array, one object per pair: "
            f'{{"id": <pair id>, "label": "<one of {", ".join(LABELS)}>", "reason": "<one sentence>"}}.\n\n{items}')


def _judge_batch(batch, model):
    proc = subprocess.run(["claude", "-p", "--model", model, "--output-format", "json", "--tools", "",
                           "--strict-mcp-config", "--no-session-persistence"],
                          input=_prompt(batch), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True, timeout=900)
    try:
        text = json.loads(proc.stdout)["result"]
        return json.loads(text[text.index("["):text.rindex("]") + 1])
    except (ValueError, KeyError) as err:  # rerunning judge resumes and retries this batch
        print(f"batch of {len(batch)} failed: {err}", file=sys.stderr)
        return []


def judge(pairs_file, out, *args):
    from concurrent.futures import ThreadPoolExecutor
    model = args[args.index("--model") + 1] if "--model" in args else "sonnet"
    pairs = [json.loads(line) for line in open(pairs_file, encoding="utf-8")]
    done = {json.loads(line)["id"] for line in open(out, encoding="utf-8")} if os.path.exists(out) else set()
    todo = [p for p in pairs if p["id"] not in done]
    batches = [todo[i:i + 20] for i in range(0, len(todo), 20)]
    with ThreadPoolExecutor(max_workers=4) as pool, open(out, "a", encoding="utf-8") as fh:
        for n, verdicts in enumerate(pool.map(lambda b: _judge_batch(b, model), batches), 1):
            for v in verdicts:
                fh.write(json.dumps(v, ensure_ascii=False) + "\n")
            fh.flush()
            print(f"judged batch {n}/{len(batches)}", flush=True)


def score(pairs_file, judged_file):
    pairs = {json.loads(line)["id"]: json.loads(line) for line in open(pairs_file, encoding="utf-8")}
    judged = {json.loads(line)["id"]: json.loads(line) for line in open(judged_file, encoding="utf-8")}
    table = collections.defaultdict(collections.Counter)
    for i, p in pairs.items():
        table[p["kind"]][judged.get(i, {}).get("label", "MISSING")] += 1
    for kind, c in sorted(table.items()):
        n = sum(c.values())
        flagged = c["UNSUPPORTED"] + c["CONTRADICTED"]
        print(f"{kind:8s} n={n:4d}  flagged {flagged / n:.3f}  " + "  ".join(f"{k} {c[k]}" for k in LABELS + ("MISSING",) if c[k]))


if __name__ == "__main__":
    {"make": make, "judge": judge, "score": score}[sys.argv[1]](*sys.argv[2:])
