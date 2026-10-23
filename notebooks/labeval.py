"""Labelled evaluation of a guardrails config, sent through rails-monitor so every verdict shows live.

Used by the lab notebooks (import it) and runnable as a script:
    CONFIG_ID=content-safety [ERRORS_AS=blocked] [STRIDE=5] python3 labeval.py

A prompt counts as blocked when the guardrails return a block. With errors_as="allowed" (the
default) an answer without a verdict counts as allowed: a client that acts only on explicit blocks,
i.e. fails open. With errors_as="blocked" it counts as a block: the client fails closed.
Standard library only.
"""
import json
import os
import secrets
import ssl
import sys
import time
import urllib.request

SA = os.environ.get("SA_DIR", "/var/run/secrets/kubernetes.io/serviceaccount")
HERE = os.path.dirname(os.path.abspath(__file__))


def namespace():
    with open(f"{SA}/namespace") as f:
        return f.read().strip()


def token():
    with open(f"{SA}/token") as f:  # re-read: the kubelet rotates it
        return f.read().strip()


def monitor_url():
    return os.environ.get("MONITOR_URL", f"https://rails-monitor.{namespace()}.svc:8443")


def load_rows(path=None, stride=1):
    """The labelled prompts: one JSON object per line with id, label (safe/unsafe) and prompt."""
    with open(path or os.environ.get("DATASET", f"{HERE}/dataset.jsonl")) as f:
        rows = [json.loads(line) for line in f if line.strip()]
    return rows[::max(int(stride), 1)]


def ratio(a, b):
    return a / b if b else 0.0


def metrics(counts):
    tp, fp, fn, tn = (counts[k] for k in ("TP", "FP", "FN", "TN"))
    return {"precision": ratio(tp, tp + fp), "recall": ratio(tp, tp + fn),
            "f1": ratio(2 * tp, 2 * tp + fp + fn), "fpr": ratio(fp, fp + tn), "fnr": ratio(fn, fn + tp)}


def evaluate(config, rows, errors_as="allowed", run=None, url=None, progress=print):
    """Send every row through the monitor; return run id, per-prompt results, counts and metrics."""
    if errors_as not in ("allowed", "blocked"):
        raise ValueError("errors_as must be 'allowed' or 'blocked'")
    run = run or f"{config}-{time.strftime('%H%M%S')}-{secrets.token_hex(2)}"  # unique even within a second
    url = (url or monitor_url()) + "/v1/chat/completions"
    ctx = ssl.create_default_context(cafile=f"{SA}/service-ca.crt") if url.startswith("https") else None
    counts, results, auth = {"TP": 0, "FP": 0, "FN": 0, "TN": 0}, [], token()
    for i, row in enumerate(rows, 1):
        body = {"model": "guardrails", "messages": [{"role": "user", "content": row["prompt"]}],
                "guardrails": {"config_id": config,
                               # input rails only: the verdict, no generation, so no main LLM is needed
                               "options": {"rails": {"input": True, "output": False, "dialog": False}}}}
        req = urllib.request.Request(url, json.dumps(body).encode(), {
            "Content-Type": "application/json", "Authorization": f"Bearer {auth}",
            "X-Eval-Run": run, "X-Eval-Label": row["label"], "X-Eval-Total": str(len(rows)),
            "X-Eval-Errors-As": errors_as})
        t = time.time()
        try:
            out = json.load(urllib.request.urlopen(req, context=ctx, timeout=90))
            od = (out.get("guardrails") or {}).get("output_data") or {}
            verdict = "error" if not od else ("blocked" if od.get("triggered_input_rail") else "allowed")
            rail = (od.get("triggered_input_rail") or "").replace(" $model=content_safety", "")
            categories = od.get("policy_violations") or []
        except Exception as e:  # refused, reset, truncated, timed out, not JSON: no verdict either way
            verdict, rail, categories = "error", type(e).__name__, []
        blocked = verdict == "blocked" or (verdict == "error" and errors_as == "blocked")
        outcome = ("TP" if blocked else "FN") if row["label"] == "unsafe" else ("FP" if blocked else "TN")
        counts[outcome] += 1
        results.append({"id": row["id"], "label": row["label"], "prompt": row["prompt"], "verdict": verdict,
                        "rail": rail, "categories": categories, "outcome": outcome,
                        "seconds": round(time.time() - t, 2)})
        if progress:
            progress(f"{i:3d}/{len(rows)} {outcome} {verdict:8s} {results[-1]['seconds']:5.1f}s {row['id']:14s} "
                     f"{rail[:30]:30s} {row['prompt'][:60]}")
    errors = sum(r["verdict"] == "error" for r in results)
    return {"run": run, "config": config, "errors_as": errors_as, "results": results,
            "counts": counts, "errors": errors, "metrics": metrics(counts)}


def summary(res):
    c, m = res["counts"], res["metrics"]
    return (f"=== {res['run']} (config {res['config']}, errors counted as {res['errors_as']}) ===\n"
            f"TP={c['TP']} FN={c['FN']} FP={c['FP']} TN={c['TN']} errors={res['errors']}\n"
            f"precision={m['precision']:.2f} recall={m['recall']:.2f} F1={m['f1']:.2f} "
            f"FPR={m['fpr']:.2f} FNR={m['fnr']:.2f}")


def main():
    config = os.environ.get("CONFIG_ID") or sys.exit("CONFIG_ID is required")
    rows = load_rows(stride=os.environ.get("STRIDE", "1"))
    errors_as = os.environ.get("ERRORS_AS", "allowed")
    if errors_as not in ("allowed", "blocked"):
        sys.exit("ERRORS_AS must be 'allowed' or 'blocked'")
    print(f"config={config} prompts={len(rows)} errors_as={errors_as}", flush=True)
    res = evaluate(config, rows, errors_as, run=os.environ.get("RUN"), progress=lambda s: print(s, flush=True))
    print(f"run={res['run']}\n\n{summary(res)}\n\nMisclassified:")
    for r in res["results"]:
        if r["outcome"] in ("FP", "FN"):
            print(f"  {r['outcome']} {r['id']:14s} {r['prompt']}")


if __name__ == "__main__":
    main()
