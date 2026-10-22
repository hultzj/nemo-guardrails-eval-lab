"""Labelled evaluation of one guardrails config, sent through rails-monitor so it shows live.

Env: CONFIG_ID (required), RUN (default: CONFIG_ID-HHMMSS), DATASET, MONITOR_URL,
ERRORS_AS ("allowed", the default: a client that only acts on an explicit block, i.e. fails open;
or "blocked": a client that refuses whenever it gets no verdict, i.e. fails closed).
Prints one line per prompt, then the confusion matrix, metrics, and every misclassified prompt.
Standard library only.
"""
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

SA = "/var/run/secrets/kubernetes.io/serviceaccount"
NS = open(f"{SA}/namespace").read().strip()
CONFIG = os.environ.get("CONFIG_ID") or sys.exit("CONFIG_ID is required")
RUN = os.environ.get("RUN") or f"{CONFIG}-{time.strftime('%H%M%S')}"
DATASET = os.environ.get("DATASET", "/data/dataset.jsonl")
URL = os.environ.get("MONITOR_URL", f"https://rails-monitor.{NS}.svc:8443") + "/v1/chat/completions"
CTX = ssl.create_default_context(cafile=f"{SA}/service-ca.crt")
ERRORS_AS = os.environ.get("ERRORS_AS", "allowed")
if ERRORS_AS not in ("allowed", "blocked"):
    sys.exit("ERRORS_AS must be 'allowed' or 'blocked'")

rows = [json.loads(line) for line in open(DATASET) if line.strip()]
token = open(f"{SA}/token").read().strip()
counts = {"TP": 0, "FP": 0, "FN": 0, "TN": 0}
errors, wrong = 0, []
print(f"run={RUN} config={CONFIG} prompts={len(rows)} errors_as={ERRORS_AS}", flush=True)

for i, row in enumerate(rows, 1):
    body = {"model": "guardrails", "messages": [{"role": "user", "content": row["prompt"]}],
            "guardrails": {"config_id": CONFIG,
                           # input rails only: the verdict, no generation, so no main LLM is needed
                           "options": {"rails": {"input": True, "output": False, "dialog": False}}}}
    req = urllib.request.Request(URL, json.dumps(body).encode(), {
        "Content-Type": "application/json", "Authorization": f"Bearer {token}",
        "X-Eval-Run": RUN, "X-Eval-Label": row["label"], "X-Eval-Total": str(len(rows)),
        "X-Eval-Errors-As": ERRORS_AS})
    t = time.time()
    try:
        out = json.load(urllib.request.urlopen(req, context=CTX, timeout=90))
        od = (out.get("guardrails") or {}).get("output_data") or {}
        verdict = "error" if not od else ("blocked" if od.get("triggered_input_rail") else "allowed")
        rail = od.get("triggered_input_rail") or ""
    except (urllib.error.URLError, ValueError, TimeoutError) as e:
        verdict, rail = "error", type(e).__name__
    errors += verdict == "error"
    blocked = verdict == "blocked" or (verdict == "error" and ERRORS_AS == "blocked")
    key = ("TP" if blocked else "FN") if row["label"] == "unsafe" else ("FP" if blocked else "TN")
    counts[key] += 1
    if key in ("FP", "FN"):
        wrong.append((key, row["id"], row["prompt"]))
    print(f"{i:3d}/{len(rows)} {key} {verdict:8s} {time.time() - t:5.1f}s {row['id']:14s} "
          f"{rail.replace(' $model=content_safety', '')[:30]:30s} {row['prompt'][:60]}", flush=True)


def ratio(a, b):
    return a / b if b else 0.0


tp, fp, fn, tn = (counts[k] for k in ("TP", "FP", "FN", "TN"))
print(f"\n=== {RUN} (config {CONFIG}, errors counted as {ERRORS_AS}) ===")
print(f"TP={tp} FN={fn} FP={fp} TN={tn} errors={errors}")
print(f"precision={ratio(tp, tp + fp):.2f} recall={ratio(tp, tp + fn):.2f} "
      f"F1={ratio(2 * tp, 2 * tp + fp + fn):.2f} FPR={ratio(fp, fp + tn):.2f} FNR={ratio(fn, fn + tp):.2f}")
print("\nMisclassified:")
for key, rid, prompt in wrong:
    print(f"  {key} {rid:14s} {prompt}")
